#!/usr/bin/env bash
# Run all the local models through the harness, then score them.
# Waits for the model pulls to finish first in case they're still downloading.
set -u
cd "$(dirname "$0")/.."

MODELS="llama3.1:8b-instruct-q4_K_M,mistral:7b-instruct-q4_K_M,phi3:mini,qwen2.5:1.5b-instruct-q4_K_M"

# wait for the pulls to land
echo "[sweep] waiting for model pulls..."
until grep -q ALL_PULLS_DONE /tmp/pulls.log 2>/dev/null; do sleep 30; done
echo "[sweep] models ready:"
ollama list

# run every local model
echo "[sweep] starting inference $(date)"
python3 harness/evaluate.py \
    --corpus harness/corpus.jsonl \
    --out harness/results.csv \
    --backends local \
    --local-model "$MODELS" \
    -k 3

# score it
echo "[sweep] aggregating $(date)"
python3 harness/aggregate.py --results harness/results.csv --out harness/evaluation.json > /tmp/agg.out 2>&1

echo "[sweep] done $(date)"
