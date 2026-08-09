#!/usr/bin/env python3
"""Analyze benchmark results and generate report content."""

import json
import sys
from pathlib import Path
from datetime import datetime


def analyze_manifest(manifest_path: Path) -> dict:
    """Analyze a single manifest file.

    Returns:
        dict with keys: model, total, passed, failed, score_pct,
                       started, finished, duration_seconds
    """
    manifest = json.loads(manifest_path.read_text())

    total = len(manifest["results"])
    passed = sum(1 for r in manifest["results"] if r["pass"])
    failed = total - passed
    score_pct = (passed / total * 100) if total > 0 else 0

    # Parse timestamps and calculate duration
    started = datetime.fromisoformat(manifest["started"])
    finished = datetime.fromisoformat(manifest["finished"])
    duration = (finished - started).total_seconds()

    return {
        "model": manifest["model"],
        "total": total,
        "passed": passed,
        "failed": failed,
        "score_pct": score_pct,
        "started": manifest["started"],
        "finished": manifest["finished"],
        "duration_seconds": duration,
        "avg_latency_seconds": duration / total if total > 0 else 0,
        "manifest_path": str(manifest_path),
        "corpus_git_sha": manifest.get("corpus_git_sha", "unknown"),
    }


def print_summary(results: list[dict]):
    """Print formatted summary of all results."""
    print("\n" + "="*70)
    print("BENCHMARK RESULTS SUMMARY")
    print("="*70)
    print(f"{'Model':<25} {'Score':<12} {'Avg Latency':<15} {'Duration'}")
    print("-"*70)

    for r in results:
        score_str = f"{r['passed']}/{r['total']} ({r['score_pct']:.1f}%)"
        latency_str = f"{r['avg_latency_seconds']:.1f}s"
        duration_str = f"{r['duration_seconds']:.1f}s"
        print(f"{r['model']:<25} {score_str:<12} {latency_str:<15} {duration_str}")

    print("\n" + "="*70)


def generate_report_section(results: list[dict], gate_threshold: int = 17) -> str:
    """Generate markdown table for report."""
    lines = []
    lines.append("| Model | Passed | Failed | Score % | Avg Latency (s) | Status |")
    lines.append("|-------|--------|--------|---------|-----------------|--------|")

    for r in results:
        status = "PASSED" if r['passed'] >= gate_threshold else "FAILED"
        if r['avg_latency_seconds'] > 90:
            status = "DISQUALIFIED (latency)"

        lines.append(
            f"| {r['model']} | {r['passed']} | {r['failed']} | "
            f"{r['score_pct']:.1f} | {r['avg_latency_seconds']:.1f} | {status} |"
        )

    return "\n".join(lines)


def main():
    """Analyze all result manifests in results/ directory."""
    results_dir = Path("results")

    if not results_dir.exists():
        print("No results directory found", file=sys.stderr)
        return 1

    # Find all manifest files (exclude benchmark-summary.json)
    manifests = [
        f for f in results_dir.glob("*.json")
        if f.name not in ["benchmark-summary.json"]
        and "holdout" not in f.name
    ]

    if not manifests:
        print("No manifest files found", file=sys.stderr)
        return 1

    # Analyze each manifest
    results = []
    for manifest_path in sorted(manifests):
        try:
            result = analyze_manifest(manifest_path)
            results.append(result)
        except Exception as e:
            print(f"Error analyzing {manifest_path}: {e}", file=sys.stderr)

    # Print summary
    print_summary(results)

    # Generate report markdown
    print("\nMarkdown for report:")
    print(generate_report_section(results))

    # Identify winner
    passed_models = [r for r in results if r['passed'] >= 17 and r['avg_latency_seconds'] <= 90]
    if passed_models:
        # Pick best score, then fastest if tied
        winner = max(passed_models, key=lambda r: (r['passed'], -r['avg_latency_seconds']))
        print(f"\nRecommended winner: {winner['model']}")
        print(f"  Score: {winner['passed']}/{winner['total']} ({winner['score_pct']:.1f}%)")
        print(f"  Avg latency: {winner['avg_latency_seconds']:.1f}s")
    else:
        print("\nNo models passed the gate (≥17/20 with ≤90s latency)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
