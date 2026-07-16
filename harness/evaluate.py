#!/usr/bin/env python3
"""Benchmark harness.

Runs the same CTI-extraction prompts through Ollama (local) and the OpenAI API
(cloud) and logs per-call timing + accuracy to a CSV. Same prompt, same corpus
for both, so the numbers reflect the model rather than the wiring around it.

No live scraping - inputs come from the pre-annotated corpus.
"""
import argparse, csv, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

ENTITIES = ["apt", "cves", "techniques", "goals", "country"]

# one prompt for both backends - don't tweak it per model or the comparison breaks
PROMPT = """You are a cyber threat intelligence analyst. Read the report text
and extract the following fields. Return ONLY a JSON object, no prose.

Fields:
- apt: list of threat actor / APT group names
- cves: list of CVE identifiers (e.g. CVE-2017-0144)
- techniques: list of MITRE ATT&CK technique names
- goals: list of the actor's goals (e.g. espionage)
- country: list of countries of origin

Report text:
\"\"\"{text}\"\"\"

JSON:"""

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# energy + cost knobs
CPU_TDP_W = 15.0           # fallback when RAPL isn't readable (laptop package)
P_KWH = 0.12               # electricity price, USD/kWh
OPENAI_RIN = 0.15          # gpt-4o-mini, USD per 1M tokens (in / out)
OPENAI_ROUT = 0.60
RAPL = "/sys/class/powercap/intel-rapl:0/energy_uj"


def read_rapl():
    try:
        return int(Path(RAPL).read_text())
    except Exception:
        return None


def parse_json(raw):
    """Grab the first {...} block out of the model's reply."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for e in ENTITIES:
        v = obj.get(e, [])
        if isinstance(v, str):
            v = [v]
        out[e] = [str(x).strip() for x in v if str(x).strip()]
    return out


def score(pred, gt):
    """TP/FP/FN per field, set comparison, case-insensitive."""
    res = {}
    for e in ENTITIES:
        p = {x.lower() for x in pred.get(e, [])}
        g = {x.lower() for x in gt.get(e, [])}
        res[e] = (len(p & g), len(p - g), len(g - p))
    return res


def f1(tp, fp, fn):
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return 2 * prec * rec / (prec + rec) if prec + rec else 0.0



def macro_f1(counts):
    return sum(f1(*counts[e]) for e in ENTITIES) / len(ENTITIES)


# Backends

def call_ollama(model, prompt):
    """Stream a completion from Ollama. Returns (text, ttft, gen_s, out_tok, in_tok)."""
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": 0.0},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    text, ttft, out_tok, in_tok = "", None, 0, 0
    start = time.perf_counter_ns()
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("response"):
                if ttft is None:
                    ttft = (time.perf_counter_ns() - start) / 1e9
                text += chunk["response"]
            if chunk.get("done"):
                out_tok = chunk.get("eval_count", 0)
                in_tok = chunk.get("prompt_eval_count", 0)
    gen = (time.perf_counter_ns() - start) / 1e9
    return text, ttft or gen, gen, out_tok, in_tok


def call_openai(model, prompt, key):
    """Same as call_ollama but against OpenAI chat completions. The
    include_usage flag makes the last SSE frame carry the real token counts,
    which we need for the cost math."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(OPENAI_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    text, ttft, out_tok, in_tok = "", None, 0, 0
    start = time.perf_counter_ns()
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            choices = chunk.get("choices") or []
            if choices:
                piece = choices[0].get("delta", {}).get("content") or ""
                if piece and ttft is None:
                    ttft = (time.perf_counter_ns() - start) / 1e9
                text += piece
            usage = chunk.get("usage")
            if usage:
                out_tok = usage.get("completion_tokens", out_tok)
                in_tok = usage.get("prompt_tokens", in_tok)
    gen = (time.perf_counter_ns() - start) / 1e9
    return text, ttft or gen, gen, out_tok, in_tok


# Progress bar

def progress_bar(done, total, model, elapsed, eta, width=28):
    """Redraw the per-model progress bar in place on stderr."""
    frac = done / total if total else 1.0
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    label = (model[:27] + "…") if len(model) > 28 else model
    sys.stderr.write(
        f"\r  {label:<28} |{bar}| {done:>3}/{total} {frac*100:5.1f}%  "
        f"elapsed {elapsed:5.0f}s  eta {eta:5.0f}s   ")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")
        sys.stderr.flush()


# Main loop


def run(records, backend, model, k, writer, fh, key=None):
    total = len(records) * k
    r_in, r_out = OPENAI_RIN, OPENAI_ROUT
    done = 0
    t_model0 = time.perf_counter_ns()
    progress_bar(0, total, model, 0, 0)
    for rec in records:
        prompt = PROMPT.format(text=rec["text"])
        prompt_bytes = len(prompt.encode())
        for run_k in range(1, k + 1):
            e0 = read_rapl()
            t_load0 = time.perf_counter_ns()
            # time the call (connect + generate) and the parse separately
            try:
                if backend == "local":
                    text, ttft, gen, out_tok, in_tok = call_ollama(model, prompt)
                else:
                    text, ttft, gen, out_tok, in_tok = call_openai(model, prompt, key)
            except (urllib.error.URLError, OSError) as ex:
                done += 1
                sys.stderr.write(f"\r  ! {rec['id'][:20]} k{run_k} failed: {ex}\n")
                el = (time.perf_counter_ns() - t_model0) / 1e9
                progress_bar(done, total, model, el,
                             el / done * (total - done) if done else 0)
                continue
            t_gen1 = time.perf_counter_ns()
            e1 = read_rapl()

            t_parse0 = time.perf_counter_ns()
            pred = parse_json(text)
            t_parse1 = time.perf_counter_ns()

            gen_s = (t_gen1 - t_load0) / 1e9
            parse_s = (t_parse1 - t_parse0) / 1e9
            latency_s = gen_s + parse_s
            tps = out_tok / gen if gen > 0 else 0.0

            # energy: prefer the RAPL counter delta, fall back to wall-time x TDP
            if e0 is not None and e1 is not None and e1 >= e0:
                energy_j = (e1 - e0) / 1e6
                energy_note = "rapl"
            elif backend == "local":
                energy_j = gen_s * CPU_TDP_W
                energy_note = "tdp_fallback"
            else:
                energy_j = 0.0
                energy_note = "cloud_na"

            odes = 0 if backend == "local" else prompt_bytes

            if backend == "local":
                c_energy = (energy_j / 3_600_000) * P_KWH
                c_billed = 0.0
            else:
                c_energy = 0.0
                c_billed = (in_tok / 1e6) * r_in + (out_tok / 1e6) * r_out
            c_total = c_energy + c_billed

            sc = score(pred, rec["ground_truth"])
            row = {
                "record_id": rec["id"], "backend": backend, "model": model,
                "run_k": run_k, "ttft_s": round(ttft, 6),
                "gen_s": round(gen_s, 6), "parse_s": round(parse_s, 6),
                "latency_s": round(latency_s, 6),
                "prompt_bytes": prompt_bytes, "input_tokens": in_tok,
                "output_tokens": out_tok, "tps": round(tps, 4),
                "energy_j": round(energy_j, 4), "energy_note": energy_note,
                "odes_bytes": odes, "c_energy": round(c_energy, 8),
                "c_billed": round(c_billed, 8), "c_total": round(c_total, 8),
                "macro_f1": round(macro_f1(sc), 6),
                "pred_json": json.dumps(pred, ensure_ascii=False),
            }
            for e in ENTITIES:
                tp, fp, fn = sc[e]
                row[f"{e}_tp"], row[f"{e}_fp"], row[f"{e}_fn"] = tp, fp, fn
            writer.writerow(row)
            fh.flush()
            done += 1
            el = (time.perf_counter_ns() - t_model0) / 1e9
            eta = el / done * (total - done) if done else 0
            progress_bar(done, total, model, el, eta)




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="harness/corpus.jsonl")
    ap.add_argument("--out", default="harness/results.csv")
    ap.add_argument("--backends", default="local")  # local/cloud
    # pass a comma-separated list to sweep several local models in one go
    ap.add_argument("--local-model",
                    default="llama3.1:8b-instruct-q4_K_M")
    ap.add_argument("--cloud-model", default="gpt-4o-mini")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    # append instead of overwrite 
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    records = [json.loads(l) for l in open(args.corpus, encoding="utf-8")]
    if args.limit:
        records = records[:args.limit]

    fields = (["record_id", "backend", "model", "run_k", "ttft_s", "gen_s",
               "parse_s", "latency_s", "prompt_bytes", "input_tokens",
               "output_tokens", "tps", "energy_j", "energy_note", "odes_bytes",
               "c_energy", "c_billed", "c_total", "macro_f1", "pred_json"]
              + [f"{e}_{m}" for e in ENTITIES for m in ("tp", "fp", "fn")])

    key = os.environ.get("OPENAI_API_KEY")
    mode = "a" if args.append and Path(args.out).exists() else "w"
    with open(args.out, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if mode == "w":
            w.writeheader()
        for b in args.backends.split(","):
            b = b.strip()
            models = ([m.strip() for m in args.local_model.split(",") if m.strip()]
                      if b == "local"
                      else [m.strip() for m in args.cloud_model.split(",") if m.strip()])
            if b == "cloud" and not key:
                print("== cloud :: skipped, OPENAI_API_KEY not set ==")
                continue
            for i, model in enumerate(models, 1):
                print(f"== [{i}/{len(models)}] {b} :: {model} "
                      f"({'openai' if b == 'cloud' else 'ollama'}) ==",
                      flush=True)
                run(records, b, model, args.k, w, f, key)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
