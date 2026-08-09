#!/bin/bash
# Run holdout evaluation against the winning model
# Usage: ./run_holdout.sh <model>

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <model>"
    echo "Example: $0 qwen3.6:35b-a3b"
    exit 1
fi

MODEL="$1"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTPUT="results/holdout-${TIMESTAMP}-$(echo $MODEL | tr ':' '-').json"

echo "Running holdout evaluation for model: $MODEL"
echo "Output will be written to: $OUTPUT"
echo ""

.venv/bin/python -m mechubbench.cli run \
    --model "$MODEL" \
    --scenarios scenarios/holdout/ \
    --tools tools/combined-tools.json \
    --endpoint http://inference.example.com:11434/v1 \
    --temperature 0.0 \
    --out "$OUTPUT"

echo ""
echo "Holdout evaluation complete!"
echo "Results: $OUTPUT"
