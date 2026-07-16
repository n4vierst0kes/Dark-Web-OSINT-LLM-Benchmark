#!/usr/bin/env python3
"""Turn results.csv into the evaluation.json the report reads from.

For every (backend, model) it works out accuracy (per-field P/R/F1, macro/micro),
speed (latency, p95, TPS), plus ODES, energy, cost, and whether it clears the
viability bar. Then it finds the Pareto frontier across configs, and - if both a
local and a cloud run are present - a paired t-test (primary local vs cloud).
"""
import argparse, csv, json, statistics as st
from collections import defaultdict

ENTITIES = ["apt", "cves", "techniques", "goals", "country"]

# what counts as "viable" (see Ch. 5)
F1_MIN = 0.30
L_MAX = 180.0      # p95 latency ceiling, seconds
ODES_MAX = 0       # no off-host bytes allowed
C_MAX = 0.01       # USD per task

PRIMARY_LOCAL = "llama3.1:8b-instruct-q4_K_M"


def f1(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return (p, r, 2 * p * r / (p + r) if p + r else 0.0)


def p95(xs):
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))
    return xs[i]


def paired_ttest(a, b):
    """Paired t-test + Cohen's d for two aligned lists (a[i] pairs with b[i])."""
    d = [x - y for x, y in zip(a, b)]
    n = len(d)
    if n < 2:
        return None
    md = st.mean(d)
    sd = st.stdev(d)
    if sd == 0:
        return {"t": float("inf"), "p": 0.0, "d": float("inf"), "n": n,
                "mean_diff": md}
    t = md / (sd / n ** 0.5)
    try:
        from scipy import stats
        p = 2 * stats.t.sf(abs(t), n - 1)
    except ImportError:
        p = float("nan")
    cohen = md / (st.pstdev(a + b) or 1)
    return {"t": t, "p": p, "d": cohen, "n": n, "mean_diff": md}


def summarize(br):
    """Roll one config's rows up into a single metrics block."""
    counts = {e: [0, 0, 0] for e in ENTITIES}
    for r in br:
        for e in ENTITIES:
            counts[e][0] += int(r[f"{e}_tp"])
            counts[e][1] += int(r[f"{e}_fp"])
            counts[e][2] += int(r[f"{e}_fn"])
    per_type, macro = {}, []
    for e in ENTITIES:
        p, rc, fc = f1(*counts[e])
        per_type[e] = {"precision": round(p, 4), "recall": round(rc, 4),
                       "f1": round(fc, 4), "tp": counts[e][0],
                       "fp": counts[e][1], "fn": counts[e][2]}
        macro.append(fc)
    macro_f1 = round(sum(macro) / len(macro), 4)
    tp = sum(counts[e][0] for e in ENTITIES)
    fp = sum(counts[e][1] for e in ENTITIES)
    fn = sum(counts[e][2] for e in ENTITIES)
    _, _, micro_f1 = f1(tp, fp, fn)

    lat = [float(r["latency_s"]) for r in br]
    tps = [float(r["tps"]) for r in br]
    ttft = [float(r["ttft_s"]) for r in br]
    energy = [float(r["energy_j"]) for r in br]
    odes = [int(r["odes_bytes"]) for r in br]
    ctotal = [float(r["c_total"]) for r in br]
    c_sum = sum(ctotal)
    c_task = c_sum / len(br) if br else 0.0

    return {
        "model": br[0]["model"],
        "backend": br[0]["backend"],
        "n_calls": len(br),
        "per_type": per_type,
        "macro_f1": macro_f1,
        "micro_f1": round(micro_f1, 4),
        "latency_mean_s": round(st.mean(lat), 3),
        "latency_p95_s": round(p95(lat), 3),
        "latency_max_s": round(max(lat), 3),
        "ttft_mean_s": round(st.mean(ttft), 3),
        "tps_mean": round(st.mean(tps), 3),
        "energy_mean_j": round(st.mean(energy), 3),
        "odes_bytes_total": sum(odes),
        "odes_bytes_mean": round(st.mean(odes), 1),
        "cost_total_usd": round(c_sum, 8),
        "cost_per_task_usd": round(c_task, 8),
        "f1_per_dollar": round(macro_f1 / c_sum, 2) if c_sum else None,
        "cost_per_tp_usd": round(c_sum / tp, 8) if tp else None,
        "_c_task": c_task,
    }


def dominates(a, b):
    """True if a beats b on every axis (higher F1, lower p95/cost/odes) and
    is strictly better on at least one - i.e. a leaves b nothing to offer."""
    obj_a = (a["macro_f1"], -a["latency_p95_s"], -a["_c_task"], -a["odes_bytes_total"])
    obj_b = (b["macro_f1"], -b["latency_p95_s"], -b["_c_task"], -b["odes_bytes_total"])
    ge = all(x >= y for x, y in zip(obj_a, obj_b))
    gt = any(x > y for x, y in zip(obj_a, obj_b))
    return ge and gt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="harness/results.csv")
    ap.add_argument("--out", default="harness/evaluation.json")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.results, encoding="utf-8")))
    # group rows by (backend, model)
    configs = {}
    for r in rows:
        configs.setdefault((r["backend"], r["model"]), []).append(r)

    report = {"configs": {}, "viability": {}, "pareto": {}, "statistics": {}}
    summaries = {}
    for (b, m), br in sorted(configs.items()):
        rep = summarize(br)
        key = f"{b}::{m}"
        summaries[key] = rep
        report["configs"][key] = {k: v for k, v in rep.items()
                                  if not k.startswith("_")}
        report["viability"][key] = {
            "macro_f1_ok": rep["macro_f1"] >= F1_MIN,
            "p95_latency_ok": rep["latency_p95_s"] <= L_MAX,
            "odes_ok": rep["odes_bytes_total"] <= ODES_MAX,
            "cost_ok": rep["_c_task"] <= C_MAX,
            "viable": (rep["macro_f1"] >= F1_MIN
                       and rep["latency_p95_s"] <= L_MAX
                       and rep["odes_bytes_total"] <= ODES_MAX
                       and rep["_c_task"] <= C_MAX),
        }

    # Pareto frontier: keep every config nothing else dominates
    keys = list(summaries)
    frontier = [k for k in keys
                if not any(dominates(summaries[o], summaries[k])
                           for o in keys if o != k)]
    report["pareto"] = {
        "objectives": "max macro_f1, min p95_latency_s, min cost_per_task_usd, min odes_bytes",
        "frontier": sorted(frontier),
        "dominated": sorted(set(keys) - set(frontier)),
    }

    # paired stats: primary local vs cloud, matched record by record
    def by_record(pred):
        acc = defaultdict(list)
        for r in rows:
            if pred(r):
                acc[r["record_id"]].append(float(r["macro_f1"]))
        return {k: st.mean(v) for k, v in acc.items()}

    backends = {r["backend"] for r in rows}
    if "local" in backends and "cloud" in backends:
        loc = by_record(lambda r: r["backend"] == "local"
                        and r["model"] == PRIMARY_LOCAL)
        if not loc:  # no primary model in the CSV, use whatever local we have
            loc = by_record(lambda r: r["backend"] == "local")
        clo = by_record(lambda r: r["backend"] == "cloud")
        ids = sorted(set(loc) & set(clo))
        report["statistics"]["macro_f1_local_vs_cloud"] = paired_ttest(
            [loc[i] for i in ids], [clo[i] for i in ids])
    else:
        report["statistics"]["note"] = (
            "paired t-test needs both backends; present: "
            f"{sorted(backends)}")

    json.dump(report, open(args.out, "w"), indent=2)
    print(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
