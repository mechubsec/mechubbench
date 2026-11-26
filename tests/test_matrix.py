"""Matrix orchestrator: retries, infra errors, restore, drift and resume."""

from __future__ import annotations

import json

import pytest

from mechubbench import matrix
from mechubbench.clients.base import AttemptUsage, ClientRun
from mechubbench.guard import Baseline
from mechubbench.inventory import BenchDevice
from mechubbench.judge import JudgeInfraError

TOKEN = "tok-SECRET-bench-agent-0123456789"
SRX = BenchDevice("bench-srx440", "srx-evo", "25.4", True)
EX = BenchDevice("bench-ex2300", "ex", "23.4", False)
SCENARIO = {"id": "read-explain", "prompt": "explain {{device}} to {{host_a}}"}


def _run(outcome: str) -> ClientRun:
    """A ClientRun whose final message tells the fake judge what to return."""
    return ClientRun(
        exit_code=0,
        final_message=outcome,
        wall_seconds=12.34,
        usage=AttemptUsage(100, 20, None, None),
        model_id="model-exact-id",
        infra_error="client crashed" if outcome == "infra" else None,
        timed_out=outcome == "timeout",
    )


class FakeDriver:
    """Driver returning scripted outcomes: pass, fail, infra, timeout, raise."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, prompt, model, mcp_url, token, timeout_s):
        self.calls.append(prompt)
        outcome = self.outcomes.pop(0)
        if outcome == "raise":
            raise ValueError("driver bug")
        return _run(outcome)


class Lab:
    """Fake guard and judge, patched into the matrix module."""

    def __init__(self, monkeypatch, drift=(), restore_fails_on=None):
        self.events = []
        self.restores = 0
        self.restore_fails_on = restore_fails_on
        monkeypatch.setattr(matrix, "check_golden", lambda *a: list(drift))
        monkeypatch.setattr(matrix, "capture_baseline", self.capture)
        monkeypatch.setattr(matrix, "plant_fault", self.plant)
        monkeypatch.setattr(matrix, "commits_since", lambda mcp, base: [])
        monkeypatch.setattr(matrix, "restore", self.restore)
        monkeypatch.setattr(matrix, "judge_attempt", self.judge)

    def capture(self, mcp, device):
        self.events.append(("baseline", device))
        return Baseline(device, "fp", "top")

    def plant(self, mcp, device, fault):
        self.events.append(("fault", device))

    def restore(self, mcp, baseline, agent_mcp=None):
        self.restores += 1
        self.events.append(("restore", baseline.device, agent_mcp))
        if self.restores == self.restore_fails_on:
            raise RuntimeError(f"restore of {baseline.device} failed")

    def judge(self, scenario, device, final_message, mcp, commits):
        if final_message == "judge-infra":
            raise JudgeInfraError("device unreachable")
        return final_message == "pass", f"judged {final_message}"


def _matrix(tmp_path, driver, scenarios=(SCENARIO,), inventory=(SRX,), **kwargs):
    return matrix.run_matrix(
        list(scenarios),
        [("codex", "gpt-6.2-astra", driver)],
        list(inventory),
        "http://bench/mcp",
        TOKEN,
        operator_mcp=object(),
        manifest_path=tmp_path / "run.json",
        server_version="2.8.1",
        golden={},
        hosts={"A": "10.66.151.10"},
        agent_mcp="agent-client",
        **kwargs,
    )


def _cell(manifest):
    [cell] = manifest["results"]
    return cell


def test_pass_first_try(tmp_path, monkeypatch):
    Lab(monkeypatch)
    driver = FakeDriver(["pass"])
    cell = _cell(_matrix(tmp_path, driver))
    assert cell["verdict"] == "pass"
    assert cell["fails_before_pass"] == 0
    assert cell["attempts"] == [
        {
            "n": 1,
            "outcome": "pass",
            "reason": "judged pass",
            "wall_s": 12.3,
            "tokens": {"input": 100, "output": 20, "cache_read": None,
                       "cache_write": None},
        }
    ]  # fmt: skip
    assert cell["model_id"] == "model-exact-id"
    assert cell["device"] == "bench-srx440" and cell["platform"] == "srx-evo"
    assert driver.calls == ["explain bench-srx440 to 10.66.151.10"]


def test_pass_after_two_fails(tmp_path, monkeypatch):
    Lab(monkeypatch)
    cell = _cell(_matrix(tmp_path, FakeDriver(["fail", "fail", "pass"])))
    assert cell["verdict"] == "pass"
    assert cell["fails_before_pass"] == 2
    assert [a["n"] for a in cell["attempts"]] == [1, 2, 3]


def test_three_fails_full_failure(tmp_path, monkeypatch):
    Lab(monkeypatch)
    driver = FakeDriver(["fail", "fail", "fail", "pass"])
    cell = _cell(_matrix(tmp_path, driver))
    assert cell["verdict"] == "full_failure"
    assert len(driver.calls) == 3


def test_infra_error_not_counted(tmp_path, monkeypatch):
    Lab(monkeypatch)
    driver = FakeDriver(["infra", "fail", "judge-infra", "pass"])
    cell = _cell(_matrix(tmp_path, driver))
    assert len(driver.calls) == 4
    assert cell["verdict"] == "pass"
    assert cell["fails_before_pass"] == 1
    assert cell["infra_errors"] == 2
    outcomes = [(a["n"], a["outcome"]) for a in cell["attempts"]]
    assert outcomes == [(1, "infra_error"), (1, "fail"), (2, "infra_error"),
                        (2, "pass")]  # fmt: skip


def test_infra_cap(tmp_path, monkeypatch):
    Lab(monkeypatch)
    driver = FakeDriver(["infra", "infra", "infra", "pass"])
    cell = _cell(_matrix(tmp_path, driver))
    assert cell["verdict"] == "infra_error"
    assert len(driver.calls) == 3


def test_timeout_counts_as_fail(tmp_path, monkeypatch):
    Lab(monkeypatch)
    cell = _cell(_matrix(tmp_path, FakeDriver(["timeout", "pass"])))
    assert cell["attempts"][0]["outcome"] == "timeout"
    assert cell["verdict"] == "pass"
    assert cell["fails_before_pass"] == 1


def test_restore_runs_after_every_attempt(tmp_path, monkeypatch):
    lab = Lab(monkeypatch)
    scenario = {**SCENARIO, "fault": "deactivate x"}
    _matrix(tmp_path, FakeDriver(["fail", "pass"]), scenarios=[scenario])
    assert lab.events == [
        ("baseline", "bench-srx440"), ("fault", "bench-srx440"),
        ("restore", "bench-srx440", "agent-client"),
    ] * 2  # fmt: skip

    lab = Lab(monkeypatch)
    with pytest.raises(ValueError):
        _matrix(tmp_path, FakeDriver(["raise"]))
    assert lab.restores == 1


def test_restore_covers_failed_fault(tmp_path, monkeypatch):
    lab = Lab(monkeypatch)

    def bad_fault(mcp, device, fault):
        raise RuntimeError("fault commit failed")

    monkeypatch.setattr(matrix, "plant_fault", bad_fault)
    driver = FakeDriver(["pass"] * 3)
    cell = _cell(_matrix(tmp_path, driver, scenarios=[{**SCENARIO, "fault": "x"}]))
    assert driver.calls == []
    assert lab.restores == 3
    assert cell["verdict"] == "infra_error"


def test_path_scope_restores_every_device(tmp_path, monkeypatch):
    lab = Lab(monkeypatch, restore_fails_on=1)
    scenario = {**SCENARIO, "device_scope": "path"}
    with pytest.raises(RuntimeError):
        _matrix(tmp_path, FakeDriver(["pass"]), [scenario], inventory=[SRX, EX])
    assert [e[:2] for e in lab.events] == [
        ("baseline", "bench-srx440"), ("baseline", "bench-ex2300"),
        ("restore", "bench-srx440"), ("restore", "bench-ex2300"),
    ]  # fmt: skip
    cell = _cell(json.loads((tmp_path / "run.json").read_text()))
    assert cell["device"] is None and cell["platform"] == "path"


def test_restore_failure_stops_run(tmp_path, monkeypatch):
    Lab(monkeypatch, restore_fails_on=2)
    driver = FakeDriver(["fail", "fail", "pass"])
    with pytest.raises(RuntimeError, match="restore"):
        _matrix(tmp_path, driver)
    assert len(driver.calls) == 2
    cell = _cell(json.loads((tmp_path / "run.json").read_text()))
    assert [a["outcome"] for a in cell["attempts"]] == ["fail", "fail"]
    assert cell["verdict"] is None


def test_drift_refuses_start(tmp_path, monkeypatch):
    Lab(monkeypatch, drift=["bench-srx345"])
    driver = FakeDriver(["pass"])
    with pytest.raises(RuntimeError, match="bench-srx345"):
        _matrix(tmp_path, driver)
    assert driver.calls == []


def test_resume_continues_numbering(tmp_path, monkeypatch):
    Lab(monkeypatch)
    second = {"id": "flow-trace", "prompt": "trace"}
    _matrix(tmp_path, FakeDriver(["pass"] + ["fail"] * 3), scenarios=[SCENARIO, second])
    path = tmp_path / "run.json"
    manifest = json.loads(path.read_text())
    manifest["results"][1]["verdict"] = None  # interrupted after attempt 1
    del manifest["results"][1]["attempts"][1:]
    path.write_text(json.dumps(manifest))

    driver = FakeDriver(["pass"])
    resumed = _matrix(tmp_path, driver, scenarios=[SCENARIO, second], resume=True)
    assert driver.calls == ["trace"]
    done, continued = resumed["results"]
    assert done["verdict"] == "pass" and len(done["attempts"]) == 1
    assert [a["n"] for a in continued["attempts"]] == [1, 2]
    assert continued["verdict"] == "pass"
    assert continued["fails_before_pass"] == 1
    assert resumed["run_id"] == manifest["run_id"]


def test_manifest_redacted_and_has_no_token(tmp_path, monkeypatch):
    Lab(monkeypatch)

    class LeakyDriver(FakeDriver):
        def run(self, prompt, model, mcp_url, token, timeout_s):
            run = super().run(prompt, model, mcp_url, token, timeout_s)
            run.final_message = f"fail Authorization: Bearer {token}"
            return run

    monkeypatch.setattr(
        matrix, "judge_attempt", lambda s, d, final, m, c: (False, final)
    )
    manifest = _matrix(tmp_path, LeakyDriver(["fail"] * 3))
    text = (tmp_path / "run.json").read_text()
    assert TOKEN not in text
    on_disk = json.loads(text)
    assert on_disk["schema_version"] == 2
    assert on_disk["server_version"] == "2.8.1"
    assert on_disk["finished"] is not None
    assert _cell(on_disk)["device"] == "bench-srx440"  # inventory names survive
    assert manifest["results"][0]["verdict"] == "full_failure"


def test_cli_matrix_wires_tokens_version_and_hermes(tmp_path, monkeypatch):
    from mechubbench import cli

    (tmp_path / "agent").write_text(TOKEN + "\n")
    (tmp_path / "operator").write_text("operator-secret\n")
    (tmp_path / "golden.json").write_text("{}")
    (tmp_path / "clients.yaml").write_text(
        "codex: {model: gpt-6.2-astra, timeout_s: 60}\n"
        "hermes: {models: ollama, ollama_url: 'http://o:11434', timeout_s: 90}\n"
    )
    scenario = tmp_path / "s.yaml"
    scenario.write_text("id: read-explain\nprompt: hi\n")

    class FakeOperator:
        def __init__(self, endpoint, token):
            assert token == "operator-secret"

        def call_tool(self, name, arguments):
            assert name == "srxmcp_status"
            return {"version": "2.8.1"}

    seen = {}

    def fake_run_matrix(scenarios, entries, devices, url, token, op, out, version,
                        golden, **kwargs):  # fmt: skip
        seen.update(entries=[(c, m) for c, m, _ in entries], token=token,
                    version=version, timeouts=kwargs["timeouts"])  # fmt: skip
        return {"results": []}

    monkeypatch.setattr(cli.runner, "MCPClient", FakeOperator)
    monkeypatch.setattr(cli, "ollama_models", lambda url: ["qwen3:32b", "gemma4"])
    monkeypatch.setattr(cli.matrix, "run_matrix", fake_run_matrix)
    args = cli.build_parser().parse_args([
        "matrix", "--scenarios", str(scenario),
        "--inventory", "bench/devices.yaml",
        "--clients-config", str(tmp_path / "clients.yaml"),
        "--mcp-endpoint", "http://bench/mcp",
        "--mcp-token-file", str(tmp_path / "agent"),
        "--setup-token-file", str(tmp_path / "operator"),
        "--golden", str(tmp_path / "golden.json"),
        "--out", str(tmp_path / "run.json"),
    ])  # fmt: skip
    assert args.func(args) == 0
    assert seen == {
        "entries": [("codex", "gpt-6.2-astra"), ("hermes", "qwen3:32b"),
                    ("hermes", "gemma4")],
        "token": TOKEN,
        "version": "2.8.1",
        "timeouts": {"codex": 60, "hermes": 90},
    }  # fmt: skip

    (tmp_path / "run.json").write_text("{}")
    assert args.func(args) == 1  # existing manifest needs --resume
