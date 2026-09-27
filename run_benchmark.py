#!/usr/bin/env python3
"""Benchmark runner for model selection.

Runs multiple models against the mechubbench corpus and collects results.
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Models to benchmark
MODELS = [
    "qwen2.5:14b-instruct",  # Floor reference
    "qwen3.6:35b-a3b",       # Candidate
    "ornith:35b",            # Candidate
    "gpt-oss:120b",          # Candidate (if fast enough)
]

# Configuration
SCENARIOS_DIR = Path("scenarios")
TOOLS_FILE = Path("tools/combined-tools.json")  # Combined Junos + PAN-OS tools
RESULTS_DIR = Path("results")
ENDPOINT = "http://mcp-infer.example.net:11434/v1"
TEMPERATURE = 0.0

# Thresholds
PASS_THRESHOLD = 17  # ≥85% of 20 scenarios
MAX_AVG_LATENCY_SECONDS = 90  # Disqualified if >90s per scenario


def run_model_benchmark(model: str) -> dict:
    """Run benchmark for a single model.

    Returns:
        dict with keys: model, total, passed, failed, avg_latency_seconds,
                       manifest_path, disqualified (bool), disqualified_reason
    """
    print(f"\n{'='*60}")
    print(f"Running benchmark: {model}")
    print(f"{'='*60}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = RESULTS_DIR / f"{timestamp}-{model.replace(':', '-')}.json"
    RESULTS_DIR.mkdir(exist_ok=True)

    cmd = [
        ".venv/bin/python", "-m", "mechubbench.cli", "run",
        "--model", model,
        "--scenarios", str(SCENARIOS_DIR),
        "--tools", str(TOOLS_FILE),
        "--endpoint", ENDPOINT,
        "--temperature", str(TEMPERATURE),
        "--out", str(output_file),
    ]

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=3600,  # 1 hour max
        )
        wall_time = time.time() - start_time
        print(result.stdout)

        # Load and analyze results
        manifest = json.loads(output_file.read_text())
        total = len(manifest["results"])
        passed = sum(1 for r in manifest["results"] if r["pass"])
        failed = total - passed

        # Calculate average latency per scenario
        avg_latency = wall_time / total if total > 0 else 0

        # Check disqualification
        disqualified = False
        disqualified_reason = None
        if avg_latency > MAX_AVG_LATENCY_SECONDS:
            disqualified = True
            disqualified_reason = f"avg latency {avg_latency:.1f}s exceeds {MAX_AVG_LATENCY_SECONDS}s threshold"

        return {
            "model": model,
            "total": total,
            "passed": passed,
            "failed": failed,
            "wall_time_seconds": wall_time,
            "avg_latency_seconds": avg_latency,
            "manifest_path": str(output_file),
            "disqualified": disqualified,
            "disqualified_reason": disqualified_reason,
        }
    except subprocess.TimeoutExpired:
        wall_time = time.time() - start_time
        return {
            "model": model,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "wall_time_seconds": wall_time,
            "avg_latency_seconds": 0,
            "manifest_path": None,
            "disqualified": True,
            "disqualified_reason": "timeout after 1 hour",
        }
    except subprocess.CalledProcessError as e:
        wall_time = time.time() - start_time
        print(f"ERROR: {e.stderr}", file=sys.stderr)
        return {
            "model": model,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "wall_time_seconds": wall_time,
            "avg_latency_seconds": 0,
            "manifest_path": None,
            "disqualified": True,
            "disqualified_reason": f"runner error: {e}",
        }


def main():
    """Run benchmarks for all models."""
    print(f"mechubbench Model Selection Benchmark")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Endpoint: {ENDPOINT}")
    print(f"Temperature: {TEMPERATURE}")
    print(f"Scenarios: {SCENARIOS_DIR}")
    print(f"Pass threshold: {PASS_THRESHOLD}/20 (85%)")
    print(f"Latency threshold: {MAX_AVG_LATENCY_SECONDS}s avg per scenario")

    all_results = []

    for model in MODELS:
        result = run_model_benchmark(model)
        all_results.append(result)

        # Print summary
        print(f"\n{model} Summary:")
        print(f"  Passed: {result['passed']}/{result['total']}")
        print(f"  Wall time: {result['wall_time_seconds']:.1f}s")
        print(f"  Avg latency: {result['avg_latency_seconds']:.1f}s per scenario")
        if result['disqualified']:
            print(f"  DISQUALIFIED: {result['disqualified_reason']}")
        elif result['passed'] >= PASS_THRESHOLD:
            print(f"  ✓ PASSED gate ({PASS_THRESHOLD}+ scenarios)")
        else:
            print(f"  ✗ FAILED gate (< {PASS_THRESHOLD} scenarios)")

    # Final summary
    print(f"\n{'='*60}")
    print(f"FINAL RESULTS")
    print(f"{'='*60}")
    print(f"{'Model':<25} {'Pass':<10} {'Latency':<15} {'Status'}")
    print(f"{'-'*60}")

    for r in all_results:
        status = "DISQUALIFIED" if r['disqualified'] else (
            "PASSED" if r['passed'] >= PASS_THRESHOLD else "FAILED"
        )
        latency_str = f"{r['avg_latency_seconds']:.1f}s" if r['avg_latency_seconds'] > 0 else "N/A"
        print(f"{r['model']:<25} {r['passed']}/{r['total']:<8} {latency_str:<15} {status}")

    # Write summary JSON
    summary_file = RESULTS_DIR / "benchmark-summary.json"
    summary_file.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "config": {
            "endpoint": ENDPOINT,
            "temperature": TEMPERATURE,
            "pass_threshold": PASS_THRESHOLD,
            "max_avg_latency_seconds": MAX_AVG_LATENCY_SECONDS,
        },
        "results": all_results,
    }, indent=2))
    print(f"\nSummary written to: {summary_file}")


if __name__ == "__main__":
    main()
