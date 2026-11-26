"""Tests for the static HTML report."""

from mechubbench.report import render_report


def _cell(client="codex", model="m1", test="t1", device="d1", verdict="pass",
          fails=0, tokens=None, wall=10.0):
    tokens = tokens or {"input": 100, "output": 50, "cache_read": 5,
                        "cache_write": 1}  # fmt: skip
    outcome = "pass" if verdict == "pass" else "fail"
    return {"client": client, "model": model, "model_id": model, "test": test,
            "device": device, "platform": "srx", "infra_errors": 0,
            "attempts": [{"n": 1, "outcome": outcome, "reason": "", "wall_s": wall,
                          "usage": tokens}],
            "verdict": verdict, "fails_before_pass": fails}  # fmt: skip


def _run(*cells):
    return {"schema_version": 2, "run_id": "run-b", "server_version": "2.8.1",
            "corpus_git_sha": "abc", "started": "2026-11-26T00:00:00Z",
            "finished": None, "results": list(cells)}  # fmt: skip


def test_full_failure_has_no_bar():
    html = render_report(_run(_cell(verdict="full_failure", fails=3)))
    assert "full failure" in html
    assert 'data-id="codex · m1|t1|run"' not in html


def test_pass_after_fails_label():
    assert "pass after 2 fails" in render_report(_run(_cell(fails=2)))


def test_null_cache_renders_blank():
    tokens = {"input": 1, "output": 2, "cache_read": None, "cache_write": None}
    html = render_report(_run(_cell(tokens=tokens)))
    assert '<td class="n"></td>' in html
    assert "None" not in html


def test_average_excludes_full_failure():
    big = {"input": 9000, "output": 1000, "cache_read": None, "cache_write": None}
    html = render_report(_run(
        _cell(device="a"), _cell(device="b", verdict="full_failure", fails=3,
                                 tokens=big)))  # fmt: skip
    assert '<td class="n">150</td>' in html
    assert "10,000" not in html


def test_ollama_flag():
    two = [_cell("hermes", "q", t, verdict="full_failure", fails=3)
           for t in ("t1", "t2")]  # fmt: skip
    one = [_cell("hermes", "ok", "t1", verdict="full_failure", fails=3),
           _cell("hermes", "ok", "t2")]  # fmt: skip
    html = render_report(_run(*two, *one))
    box = html.split('class="flag"')[1].split("</div>")[0]
    assert "hermes · q" in box
    assert "hermes · ok" not in box


def test_compare_mismatched_runs():
    run = _run(_cell(model="new"))
    base = _run(_cell(model="old"), _cell(model="new", test="gone"))
    html = render_report(run, base)
    svgs = [chunk.split("</svg>")[0] for chunk in html.split("<svg")[1:]]
    t1_svg = next(x for x in svgs if "codex · old" in x and "codex · new" in x)
    assert "run —" in t1_svg  # old: cell only in baseline
    assert "base —" in t1_svg  # new: cell only in run
    assert "codex · new" in t1_svg  # run-only model label survives


def test_no_external_urls():
    html = render_report(_run(_cell()), _run(_cell()))
    assert "http://" not in html and "https://" not in html


def test_escapes_manifest_strings():
    assert "<script>" not in render_report(_run(_cell(model="<script>")))


def test_legend_and_prefixes_with_baseline():
    html = render_report(_run(_cell()), _run(_cell()))
    assert "grey = baseline run-b, blue = this run run-b" in html
    assert "no data in that run" in html
    assert ">base " in html and ">run " in html
    assert "grey =" not in render_report(_run(_cell()))


def test_labels_fit_viewbox():
    import re

    cells = [_cell(device="a", fails=3),
             _cell(device="b", verdict="full_failure", fails=3)]  # fmt: skip
    html = render_report(_run(*cells), _run(*cells))
    svg = html.split("<svg")[1].split("</svg>")[0]
    width = float(re.search(r'viewBox="0 0 ([\d.]+)', html).group(1))
    for x, text in re.findall(r'<text x="([\d.]+)"[^>]*>([^<]*)</text>', svg):
        assert float(x) + 6.5 * len(text) <= width, text


def test_averages_pass_rate_and_per_test_mean():
    cheap = {"input": 10, "output": 0, "cache_read": None, "cache_write": None}
    dear = {"input": 1000, "output": 0, "cache_read": None, "cache_write": None}
    cells = [_cell(model="x", test="t1", tokens=cheap)]
    cells += [_cell(model="x", test=f"t{i}", verdict="full_failure", fails=3)
              for i in (2, 3, 4, 5)]  # fmt: skip
    cells += [_cell(model="y", test=f"t{i}", tokens=dear) for i in (1, 2, 3, 4, 5)]
    html = render_report(_run(*cells))
    assert '<td class="n">1/5</td>' in html and '<td class="n">5/5</td>' in html
    assert "then mean of tests passed" in html


def test_infra_error_style_and_rate():
    cells = [_cell(device="a"), _cell(device="b", verdict="infra_error")]
    html = render_report(_run(*cells))
    assert '<td class="n">1/1</td>' in html
    assert 'class="infra_error"' in html
    only = render_report(_run(_cell(verdict="infra_error")))
    assert "infra error" in only and "full failure" not in only
    assert 'class="hatch"' not in only


def test_all_null_tokens_is_no_data_not_zero():
    tokens = {"input": None, "output": None, "cache_read": None, "cache_write": None}
    html = render_report(_run(_cell(tokens=tokens)))
    assert 'data-id="codex \u00b7 m1|t1|run"' in html  # wall bar still drawn
    tokens_svg = html.split("<svg")[1].split("</svg>")[0]
    assert "data-id" not in tokens_svg and "0 pass" not in tokens_svg
