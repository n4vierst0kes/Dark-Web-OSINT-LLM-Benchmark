# Results — Local Pipeline (llama3.1:8b-instruct-q4_K_M)

Executed run of the Chapter 4 harness over the full 86-record corpus, k=3
(258 calls), temperature 0.0, on the CPU-bound host. Cloud (OpenAI) column not
run — no API key available this session — so the paired t-test / Cohen's d are
left for a run with both backends present.

## Extraction accuracy (5.2)

| Entity | Precision | Recall | F1 | TP | FP | FN |
|--------|-----------|--------|------|-----|-----|------|
| apt | 0.953 | 0.191 | 0.318 | 243 | 12 | 1029 |
| cves | 0.000 | 0.000 | 0.000 | 0 | 0 | 717 |
| techniques | 0.000 | 0.000 | 0.000 | 0 | 15 | 3756 |
| goals | 0.632 | 0.779 | 0.698 | 201 | 117 | 57 |
| country | 0.420 | 0.488 | 0.452 | 126 | 174 | 132 |

- **Macro-F1 = 0.294** (headline metric)
- **Micro-F1 = 0.160**

## System performance (5.3)

| Metric | Value |
|--------|-------|
| Mean end-to-end latency | 13.0 s |
| p95 latency | 20.1 s |
| Mean TTFT | 2.34 s |
| Mean TPS | 3.77 t/s |

p95 latency (20 s) sits far under the **180 s** viability target for CPU-bound
SLM inference.

## OpSec + cost (5.4)

| Metric | Value |
|--------|-------|
| ODES (bytes off-host) | 0 |
| Mean energy/call (TDP fallback) | 195 J |
| Total local cost | $0.00168 |
| F1-per-dollar | 174.9 |
| Cost-per-TP | $2.9e-06 |

RAPL was not exposed in the run environment, so energy is the documented
wall×TDP fallback (15 W package). ODES = 0 by construction.

## Viability (5.5 / III-G)

| Constraint | Threshold | Result | Pass |
|-----------|-----------|--------|------|
| macro-F1 ≥ F1_min | 0.30 (analyst-set) | 0.294 | ✗ (boundary) |
| p95 ≤ L_max | 180 s | 20.1 s | ✓ |
| ODES ≤ ODES_max | 0 | 0 | ✓ |
| C_total ≤ C_max | $0.01/task | ~$6.5e-06 | ✓ |

The pipeline clears every operational gate except accuracy, and lands right on
the accuracy boundary (0.294 vs a 0.30 bar; viable under any bar ≤ 0.29).
F1_min is analyst-defined — the report fixes no numeric value — so viability is
threshold-conditional, which is the report's intended framing.

## Discussion

- **apt precision 0.95 / recall 0.19.** When the model names an actor it is
  almost always a ground-truth alias (alias expansion working as designed —
  e.g. APT12→"Numbered Panda" scores TP). Recall is low because the reference
  set merges *every* known alias per actor (mean ~15 aliases), while the model
  emits ~1 name per record; each unnamed alias is an FN. This is the specified
  set-intersection scoring, not a defect.
- **cves / techniques = 0.** These entities never appear in the actor
  descriptions (0/86 records), so recall is structurally bounded at zero.
  Critically, `techniques` shows 15 false positives — the model invented
  technique names — directly exhibiting the hallucination-on-CTI reliability
  concern the report cites [5]. Correct behaviour here is to emit nothing.
- **goals F1 0.70, country F1 0.45** are the well-supported fields and drive
  the macro score.
- **Determinism.** With temperature 0.0, per-record k=3 outputs were identical
  (e.g. APT16 macro-F1 0.300 across all three repeats), so residual
  non-determinism on this local backend is negligible — consistent with
  greedy decoding on a fixed local model.

## To complete the local-vs-cloud comparison

```bash
export OPENAI_API_KEY=...
python3 evaluate.py --backends local,cloud --local-model llama3.1:8b-instruct-q4_K_M
python3 aggregate.py     # emits paired t-test + Cohen's d on macro-F1
```
