# mechubbench Results

This directory contains benchmark run manifests and the model selection record.

## Files

- `YYYY-MM-DD-HHMMSS-<model>.json`: Individual run manifests (one per model evaluation)
- `holdout-<timestamp>-<model>.json`: Holdout evaluation manifests (run once post-selection)
- `benchmark-summary.json`: Aggregated results from `run_benchmark.py`
- `2026-08-09-selection.md`: Model selection decision record (Task 4)

## Manifest Schema

Each manifest file follows this structure:

```json
{
  "schema_version": 1,
  "run_id": "uuid",
  "model": "model-name",
  "corpus_git_sha": "git-sha",
  "started": "ISO8601-timestamp",
  "finished": "ISO8601-timestamp",
  "results": [
    {
      "id": "scenario-id",
      "pass": true/false,
      "reason": "scoring-rule-or-error",
      "started": "ISO8601-timestamp",
      "finished": "ISO8601-timestamp",
      "transcript": [
        {"tool": "tool-name", "args": {...}}
      ]
    }
  ]
}
```

## Analysis

Use `analyze_results.py` to generate summary statistics and markdown tables for the report.
