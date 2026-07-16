# CTI Extraction Benchmark Harness — Local vs. Cloud LLM Pipelines

Executes the Chapter 4 methodology and Chapter 5 evaluation from the report
*Dark Web OSINT: Evaluating Local and Cloud LLM Pipelines for Dark Web Intelligence*.

The harness routes one fixed CTI-extraction prompt over one static corpus
through two backends (local Ollama, cloud OpenAI) so any measured variance is
the model's, not the plumbing. Live Tor scraping is stubbed (simulated-hook
mode): records are read straight from the pre-annotated corpus.

## Files

| File | Role |
|------|------|
| `build_corpus.py` | Builds the static ground-truth corpus from the Di Tizio APT bundle |
| `corpus.jsonl` | 86 records, one per MITRE ATT&CK threat actor (byte-identical across runs) |
| `evaluate.py` | Inference loop + per-call telemetry (Stage 3–5) |
| `results.csv` | Per-call telemetry, one row per (record, backend, k) |
| `aggregate.py` | Chapter 5 scoring: P/R/F1, macro/micro-F1, latency, ODES, cost, stats |
| `evaluation.json` | Final metrics |

## Run

```bash
python3 build_corpus.py                     # -> corpus.jsonl
ollama serve &                              # local backend
python3 evaluate.py --backends local \
    --local-model llama3.1:8b-instruct-q4_K_M -k 3
# cloud backend (needs a key):
export OPENAI_API_KEY=...
python3 evaluate.py --backends local,cloud
python3 aggregate.py                        # -> evaluation.json
```

## How the code maps to the report

- **Stage 2 model config** — `--local-model` (Ollama Q4_K_M GGUF) and
  `--cloud-model` (OpenAI REST). Adapters `call_ollama` / `call_openai`.
- **Stage 3 simulated hook** — corpus read from `corpus.jsonl`, no Tor.
- **Stage 4 determinism** — `temperature=0.0`, `k=3` repeats per record.
- **Stage 5 timing** — `time.perf_counter_ns()` brackets generation and
  parsing separately; TTFT is time to first streamed token; energy via Intel
  RAPL with a wall×TDP fallback.
- **5.2 accuracy** — `parse_json` recovers entities, `score` does
  case-insensitive set TP/FP/FN, macro-F1 is the headline metric.
- **5.3 performance** — latency mean/p95, TPS, TTFT.
- **5.4 ODES** — `0` local, `prompt_bytes` cloud.
- **5.5 stats** — paired t-test + Cohen's d on per-record macro-F1.

## Corpus note (stated limitation)

The bundle's structured ground truth (ThreatActors, aliases, CVEs, MITRE
techniques, country, goals) is anchored to 86 MITRE ATT&CK actors; there is no
clean report-PDF→actor annotation, so each actor's MITRE description is the
input document. Consequently the `apt`, `country`, and `goals` fields have
strong textual support while `cves` and `techniques` are not present in the
description prose — for those two fields the correct behaviour is to emit
nothing, and any emitted value is a hallucination counted as a false positive.
This makes the corpus a direct probe of the hallucination-reliability concern
raised in the report, at the cost of `cves`/`techniques` recall being bounded
near zero. Both backends see byte-identical input, so the local-vs-cloud
comparison remains valid.
