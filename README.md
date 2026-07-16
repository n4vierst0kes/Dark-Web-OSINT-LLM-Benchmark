# Local vs. Cloud LLM Pipelines for Dark Web OSINT - Evaluation Harness

Reproducibility harness for the report
**_Dark Web OSINT: Evaluating Local and Cloud LLM Pipelines for Dark Web Intelligence_**.

It routes one fixed CTI-extraction prompt over one static, pre-annotated corpus
through two backends - a local model via **Ollama** and a cloud model via the
**OpenAI** REST API - so that any measured variance is attributable to the
model, not the plumbing. Live Tor scraping is stubbed (simulated-hook mode):
records are read straight from the pre-annotated corpus.

## Repository layout

```
harness/
  build_corpus.py    Build the static ground-truth corpus from the Di Tizio APT bundle
  corpus.jsonl       86 records, one per MITRE ATT&CK threat actor (byte-identical across runs)
  evaluate.py        Inference loop + per-call telemetry (Stage 3–5)
  results.csv        Per-call telemetry, one row per (record, backend, k)
  aggregate.py       Chapter 5 scoring: P/R/F1, macro/micro-F1, latency, ODES, cost, stats
  evaluation.json    Final aggregated metrics
  inject_results.py  Writes measured numbers back into the report
  build_docx.py      Regenerates the report .docx from source
  run_sweep.sh       Runs every local model through the harness, then scores it
  README.md          Detailed methodology-to-code mapping
  RESULTS.md         Human-readable results summary
```

## Setup

```bash
pip install -r requirements.txt
```

## Quick start

```bash
cd harness
python3 build_corpus.py                     # -> corpus.jsonl  (needs the dataset, see below)
ollama serve &                              # local backend
python3 evaluate.py --backends local \
    --local-model llama3.1:8b-instruct-q4_K_M -k 3

export OPENAI_API_KEY=...                    # cloud backend
python3 evaluate.py --backends local,cloud
python3 aggregate.py                        # -> evaluation.json
```

The pre-built `corpus.jsonl`, `results.csv`, and `evaluation.json` are committed,
so `aggregate.py` reproduces the reported metrics without re-running inference.

## Dataset (not committed)

The corpus is derived from the MIT-licensed **APTs database** by Giorgio Di
Tizio (paper: *Software Updates Strategies: a Quantitative Evaluation against
Advanced Persistent Threats*). The ~577 MB bundle is **not** included here - set
it up locally before running `build_corpus.py`:

```bash
# obtain the APTs-database release, then:
unzip APTs-database-v1.0.0.zip -d _extract
python3 harness/build_corpus.py _extract/giorgioditizio-APTs-database-<hash>
```

## Corpus note (stated limitation)

The bundle's structured ground truth is anchored to 86 MITRE ATT&CK actors.
Each actor's MITRE description is the input document, so the `apt`, `country`,
and `goals` fields have strong textual support while `cves` and `techniques` do
not appear in the description prose - for those two fields the correct behaviour
is to emit nothing, and any emitted value is a hallucination counted as a false
positive. This makes the corpus a direct probe of the hallucination-reliability
concern raised in the report, at the cost of `cves`/`techniques` recall being
bounded near zero. Both backends see byte-identical input, so the
local-vs-cloud comparison remains valid.

See `harness/README.md` for the full stage-by-stage mapping to the report.
