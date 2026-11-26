"""Static HTML report for a schema-v2 matrix manifest (optionally vs a baseline)."""

from __future__ import annotations

from html import escape
from statistics import mean

FOOTNOTE = (
    "Clients count tokens slightly differently; cache columns are blank "
    "where a client does not report them."
)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")

CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5b6570;--line:#d0d7de;--bar:#2a6fdb;
--base:#9aa7b4;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--box:#fff8c5}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;
--muted:#9aa4af;--line:#30363d;--bar:#5b9dff;--base:#6e7b88;--ok:#3fb950;
--bad:#ff7b72;--warn:#d29922;--box:#3a2f0b}}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;
margin:0 auto;padding:0 16px 32px;max-width:900px}
h1{font-size:1.4em}h2{font-size:1.15em;margin-top:2em}
.muted,.note{color:var(--muted);font-size:.9em}
.charts{display:flex;flex-wrap:wrap;gap:12px}.charts figure{flex:1 1 300px;margin:0}
figcaption{font-weight:600;font-size:.9em}svg{width:100%;height:auto}
svg text{fill:var(--fg);font:11px system-ui,sans-serif}
.v{fill:var(--muted)}.bar{fill:var(--bar)}.basebar{fill:var(--base)}
.hatch{stroke:var(--bad);stroke-width:2}.fail{fill:var(--bad)}
.wrap{overflow-x:auto}table{border-collapse:collapse;font-size:.9em}
th,td{border:1px solid var(--line);padding:3px 8px;text-align:left}
td.n{text-align:right}.pass{color:var(--ok)}.full_failure{color:var(--bad)}
.flag{background:var(--box);border:1px solid var(--warn);padding:8px 12px}
"""


def _label(result: dict) -> str:
    return f"{result['client']} · {result['model']}"


def _num(value: float | None, digits: int = 0) -> str:
    return "" if value is None else f"{value:,.{digits}f}"


def _passing_attempt(cell: dict) -> dict | None:
    return next((a for a in cell["attempts"] if a["outcome"] == "pass"), None)


def _total_tokens(attempt: dict) -> int:
    tokens = attempt.get("tokens") or {}
    return (tokens.get("input") or 0) + (tokens.get("output") or 0)


def _group(run: dict | None) -> dict:
    """Index results as {(label, test): [cells]}, plus label/test order."""
    index: dict[tuple[str, str], list[dict]] = {}
    for cell in (run or {}).get("results", []):
        index.setdefault((_label(cell), cell["test"]), []).append(cell)
    return index


def _aggregate(cells: list[dict] | None) -> dict | None:
    """Collapse a model x test group into bar values and a label."""
    if not cells:
        return None
    passes = [c for c in cells if c["verdict"] == "pass"]
    full_failures = sum(c["verdict"] == "full_failure" for c in cells)
    if not passes:
        text = "full failure" if full_failures else "infra error"
        return {"tokens": None, "wall": None, "label": text, "passes": 0,
                "total": len(cells)}  # fmt: skip
    attempts = [_passing_attempt(c) for c in passes]
    fails = sum(c["fails_before_pass"] for c in passes)
    label = f"pass after {fails} fails" if fails else "pass"
    if full_failures:
        label += f" ({full_failures} full failure)"
    return {
        "tokens": mean(_total_tokens(a) for a in attempts),
        "wall": mean(a["wall_s"] for a in attempts),
        "label": label,
        "passes": len(passes),
        "total": len(cells),
    }


def _chart(title: str, rows: list, key: str, unit: str, test: str) -> str:
    """Horizontal bars; rows are (label, this-run agg, baseline agg)."""
    values = [
        agg[key] for _, run_agg, base_agg in rows
        for agg in (run_agg, base_agg) if agg and agg[key] is not None
    ]  # fmt: skip
    top = max(values, default=0) or 1
    width, bar_max, height = 360, 230, 0
    parts = []
    for label, run_agg, base_agg in rows:
        parts.append(
            f'<text x="0" y="{height + 11}">{escape(label)}</text>'
        )
        height += 15
        series = [("basebar", "baseline", base_agg)] if base_agg is not None else []
        if run_agg is not None or base_agg is not None:
            series.append(("bar", "run", run_agg))
        for css, who, agg in series:
            if agg is None:
                parts.append(f'<text x="0" y="{height + 10}" class="v">—</text>')
            elif agg[key] is None:
                parts.append(
                    f'<rect x="0" y="{height}" width="{bar_max}" height="11" '
                    f'fill="none" class="hatch" stroke-dasharray="3 3"/>'
                    f'<text x="{bar_max + 6}" y="{height + 10}" class="fail">'
                    f'{escape(agg["label"])}</text>'
                )
            else:
                bar = max(2, agg[key] / top * bar_max)
                data_id = escape(f"{label}|{test}|{who}", quote=True)
                parts.append(
                    f'<rect class="{css}" data-id="{data_id}" x="0" '
                    f'y="{height}" width="{bar:.1f}" height="11"/>'
                    f'<text x="{bar + 6:.1f}" y="{height + 10}" class="v">'
                    f'{_num(agg[key], 1 if unit == "s" else 0)}{unit} '
                    f'{escape(agg["label"])}</text>'
                )
            height += 14
        height += 6
    return (
        f'<figure><figcaption>{escape(title)}</figcaption>'
        f'<svg viewBox="0 0 {width} {max(height, 1)}" role="img" '
        f'aria-label="{escape(title, quote=True)}">{"".join(parts)}</svg></figure>'
    )


def _device_table(cells: list[dict]) -> str:
    rows = []
    for cell in cells:
        attempt = _passing_attempt(cell) or (cell["attempts"] or [None])[-1]
        tokens = (attempt or {}).get("tokens") or {}
        cols = "".join(f'<td class="n">{_num(tokens.get(k))}</td>' for k in TOKEN_KEYS)
        wall = _num(attempt["wall_s"], 1) if attempt else ""
        verdict = cell["verdict"] or ""
        rows.append(
            f"<tr><td>{escape(_label(cell))}</td>"
            f"<td>{escape(str(cell['device'] or ''))}</td>"
            f"<td>{escape(str(cell['platform'] or ''))}</td>"
            f'<td class="{escape(verdict, quote=True)}">{escape(verdict)}</td>'
            f'{cols}<td class="n">{wall}</td></tr>'
        )
    head = (
        "<tr><th>model</th><th>device</th><th>platform</th><th>verdict</th>"
        "<th>input</th><th>output</th><th>cache read</th><th>cache write</th>"
        "<th>wall s</th></tr>"
    )
    return f'<div class="wrap"><table>{head}{"".join(rows)}</table></div>'


def _rate(cells: list[dict] | None) -> str:
    if not cells:
        return "—"
    return f"{sum(c['verdict'] == 'pass' for c in cells)}/{len(cells)}"


def _averages(run: dict, labels: list[str]) -> str:
    """Per-model mean over passing cells only (full failures and infra excluded)."""
    rows = []
    for label in labels:
        attempts = [
            _passing_attempt(c) for c in run["results"]
            if _label(c) == label and c["verdict"] == "pass"
        ]  # fmt: skip
        if not attempts:
            rows.append(f"<tr><td>{escape(label)}</td><td></td><td></td></tr>")
            continue
        tokens = mean(_total_tokens(a) for a in attempts)
        wall = mean(a["wall_s"] for a in attempts)
        rows.append(
            f'<tr><td>{escape(label)}</td><td class="n">{tokens:,.0f}</td>'
            f'<td class="n">{wall:.1f}</td></tr>'
        )
    return (
        '<div class="wrap"><table><tr><th>model</th><th>avg tokens</th>'
        f'<th>avg wall s</th></tr>{"".join(rows)}</table></div>'
    )


def _ollama_flag(run: dict) -> str:
    """List hermes (Ollama) models with full failures on more than one test."""
    failed: dict[str, set[str]] = {}
    for cell in run["results"]:
        if cell["client"] == "hermes" and cell["verdict"] == "full_failure":
            failed.setdefault(_label(cell), set()).add(cell["test"])
    flagged = sorted(label for label, tests in failed.items() if len(tests) > 1)
    if not flagged:
        return ""
    items = "".join(f"<li>{escape(label)}</li>" for label in flagged)
    return (
        '<div class="flag"><strong>Ollama models with full failures on more '
        f"than one test</strong><ul>{items}</ul></div>"
    )


def render_report(run: dict, baseline: dict | None = None) -> str:
    """Render the run (and optional baseline) as one self-contained HTML page.

    Args:
        run: Schema-v2 manifest of the run being reported.
        baseline: Optional earlier manifest; shown beside the run, with
            ``—`` wherever a model/test cell exists in only one run.

    Returns:
        The complete HTML document as a string.
    """
    this_run, base_run = _group(run), _group(baseline)
    tests = sorted({t for _, t in this_run} | {t for _, t in base_run})
    labels = sorted({m for m, _ in this_run} | {m for m, _ in base_run})
    runs = [run] + ([baseline] if baseline else [])
    versions = ", ".join(escape(str(r.get("server_version"))) for r in runs)
    ids = " vs ".join(escape(str(r.get("run_id"))) for r in reversed(runs))

    sections = []
    for test in tests:
        rows = [
            (label, _aggregate(this_run.get((label, test))),
             _aggregate(base_run.get((label, test))) if baseline else None)
            for label in labels
        ]  # fmt: skip
        # Baseline-only cells still need a row: mark the run side missing.
        charts = (
            _chart("Total tokens", rows, "tokens", "", test)
            + _chart("Wall seconds", rows, "wall", "s", test)
        )
        cells = [c for c in run["results"] if c["test"] == test]
        sections.append(
            f"<h2>{escape(test)}</h2><div class=\"charts\">{charts}</div>"
            f"<details><summary>Devices</summary>{_device_table(cells)}</details>"
        )

    grid = []
    for label in labels:
        cols = []
        for test in tests:
            mine = _rate(this_run.get((label, test)))
            text = mine if not baseline else (
                f"{mine} <span class=\"muted\">(baseline "
                f"{_rate(base_run.get((label, test)))})</span>"
            )
            cols.append(f"<td>{text}</td>")
        grid.append(f"<tr><td>{escape(label)}</td>{''.join(cols)}</tr>")
    head = "".join(f"<th>{escape(t)}</th>" for t in tests)
    summary = (
        f'<div class="wrap"><table><tr><th>model</th>{head}</tr>'
        f'{"".join(grid)}</table></div>'
    )

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>mechubbench report</title><style>{CSS}</style></head><body>"
        f"<h1>mechubbench report</h1>"
        f'<p class="muted">Run {ids}<br>Server {versions} &middot; '
        f'{escape(str(run.get("started")))}<br>'
        f'{"; ".join(escape(m) for m in labels)}</p>'
        f"{_ollama_flag(run)}<h2>Summary</h2>{summary}"
        f"<h2>Averages (passes only)</h2>{_averages(run, labels)}"
        f'{"".join(sections)}<p class="note">{escape(FOOTNOTE)}</p></body></html>'
    )
