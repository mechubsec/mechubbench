#!/usr/bin/env python3
"""Re-apply mechubbench.redact's current masker to committed results/*.json.

`cli.py` redacts a manifest once, at the masker version current when the run
happened. When the masker gains coverage (e.g. MEC-192's allowlist-shaped
IP/hostname/serial/username masking), files committed under an older masker
keep whatever it missed. This script re-runs the *current* masker over every
committed results file, in place, so HEAD always reflects the latest
redaction rules without needing to re-run the benchmark.

Never hand-edit results/*.json for this: the masker's rules are the single
source of truth for what is safe to keep, and hand edits can't be checked
for completeness the way re-running the masker can.

Redaction never touches scoring (`cli.py` redacts only after scoring), but
this script double-checks that invariant per file and refuses to write a
file whose pass/fail counts would change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mechubbench import core, redact  # noqa: E402

RESULTS_DIR = REPO_ROOT / "results"
SCENARIOS_DIR = REPO_ROOT / "scenarios"


def _allowed_literals(scenarios_dir: Path) -> set[str]:
    """Union of every scenario's `expected_literals`, mirroring cli.py."""
    allowed: set[str] = set()
    if not scenarios_dir.is_dir():
        return allowed
    for scenario in core.load_scenarios(scenarios_dir):
        allowed.update(scenario.get("expected_literals") or [])
    return allowed


def _pass_counts(manifest: dict) -> tuple[int, int]:
    results = manifest.get("results", [])
    passed = sum(1 for r in results if r.get("pass"))
    return passed, len(results)


def redact_file(path: Path, allowed_literals: set[str]) -> bool:
    """Re-redact one results file in place.

    Returns:
        True if the file's content changed, False if it already matched
        what the current masker would produce.

    Raises:
        RuntimeError: if re-redaction would change the file's pass/fail
            counts. redact_manifest must never affect scoring; a mismatch
            here means either the masker or this script has a bug.
    """
    original_text = path.read_text()
    manifest = json.loads(original_text)
    before = _pass_counts(manifest)

    redacted = redact.redact_manifest(manifest, allowed_literals=allowed_literals)

    after = _pass_counts(redacted)
    if before != after:
        raise RuntimeError(
            f"{path.name}: pass/fail counts changed ({before} -> {after}) "
            "while re-redacting; refusing to write"
        )

    # Match cli.py's own write format (json.dumps(..., indent=2), no
    # trailing newline) so a file the masker leaves untouched round-trips
    # byte-for-byte and a second run reports no diff.
    new_text = json.dumps(redacted, indent=2)
    if new_text == original_text:
        return False
    path.write_text(new_text)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="Directory of results/*.json to re-redact (default: results/)",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=SCENARIOS_DIR,
        help="Scenario directory to read expected_literals from (default: scenarios/)",
    )
    args = parser.parse_args(argv)

    allowed_literals = _allowed_literals(args.scenarios_dir)

    paths = sorted(args.results_dir.glob("*.json"))
    changed = []
    for path in paths:
        if redact_file(path, allowed_literals):
            changed.append(path.name)

    print(f"Checked {len(paths)} file(s) in {args.results_dir}.")
    if changed:
        print(f"Re-redacted {len(changed)} file(s):")
        for name in changed:
            print(f"  {name}")
    else:
        print("No changes; every file already matches the current masker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
