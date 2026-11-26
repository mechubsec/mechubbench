"""Tests for the headless client drivers (fixtures captured from real CLIs)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from mechubbench.clients import claude_code
from mechubbench.clients.base import run_in_group, write_back
from mechubbench.clients.claude_code import ClaudeCodeDriver, parse_claude
from mechubbench.clients.codex import CodexDriver, isolated_catalog, parse_codex
from mechubbench.clients.hermes import HermesDriver, ollama_models, parse_hermes

FIXTURES = Path(__file__).parent / "fixtures" / "clients"


def triple(parsed):
    """(final, usage, model_id) of a parser result."""
    return parsed.final, parsed.usage, parsed.model_id


def fixture(name: str) -> str:
    """Return the text of a captured client fixture."""
    return (FIXTURES / name).read_text()


class FakeRunner:
    """Stands in for subprocess.run and records the call."""

    def __init__(self, stdout: str = "", returncode: int = 0, raise_timeout=False):
        self.stdout = stdout
        self.returncode = returncode
        self.raise_timeout = raise_timeout
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.home_files: list[str] = []

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.env = kwargs["env"]
        self.home_files = sorted(
            str(path.relative_to(kwargs["cwd"]))
            for path in Path(kwargs["cwd"]).rglob("*")
        )
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=self.stdout)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def fake_claude_auth(tmp_path: Path) -> Path:
    """Create a stand-in Claude credentials file."""
    creds = tmp_path / "real-creds.json"
    creds.write_text("{}")
    return creds


def fake_models_cache(tmp_path: Path) -> Path:
    """Create a stand-in Codex models cache with one code-mode model."""
    cache = tmp_path / "models_cache.json"
    model = {
        "slug": "gpt-x",
        "tool_mode": "code_mode_only",
        "apply_patch_tool_type": "f",
    }
    cache.write_text(json.dumps({"identity": "acct", "models": [model]}))
    return cache


def codex_driver(tmp_path: Path, runner: FakeRunner) -> CodexDriver:
    """Build a CodexDriver wired to stand-in auth and catalog files."""
    return CodexDriver(
        tmp_root=tmp_path,
        runner=runner,
        auth=fake_claude_auth(tmp_path),
        models_cache=fake_models_cache(tmp_path),
    )


def test_isolated_catalog_forces_direct_mode(tmp_path):
    catalog = isolated_catalog(fake_models_cache(tmp_path))
    assert catalog == {
        "models": [
            {
                "slug": "gpt-x",
                "tool_mode": "direct",
                "experimental_supported_tools": [],
                "node_repl_disabled": True,
                "supports_search_tool": False,
            }
        ]
    }


def test_parse_claude_ok():
    final, usage, model_id = triple(parse_claude(fixture("claude_ok.json")))
    assert "ok" in final
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert isinstance(usage.cache_read_tokens, int)
    assert isinstance(usage.cache_write_tokens, int)
    assert model_id


def test_parse_codex_sums_turns():
    turn = {
        "type": "turn.completed",
        "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3},
    }
    message = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "hi"},
    }
    stdout = "\n".join(json.dumps(event) for event in (turn, message, turn))
    final, usage, _ = triple(parse_codex(stdout))
    assert final == "hi"
    # Codex's input_tokens includes the cached prefix; input is the uncached part.
    assert (usage.input_tokens, usage.output_tokens) == (12, 6)
    assert usage.cache_read_tokens == 8
    assert usage.cache_write_tokens is None  # not reported in these events


def test_parse_codex_fixture():
    final, usage, _ = triple(parse_codex(fixture("codex_ok.jsonl")))
    assert final == "ok"
    assert usage.input_tokens == 13509 - 11648
    assert usage.cache_read_tokens == 11648
    assert usage.cache_write_tokens == 0  # reported as cache_write_input_tokens


def test_parse_hermes_ok(tmp_path):
    usage_file = tmp_path / "usage.json"
    report = {"input_tokens": 7, "output_tokens": 2, "cache_read_tokens": 5}
    usage_file.write_text(json.dumps(report))
    parsed = parse_hermes(fixture("hermes_ok.jsonl"), usage_file)
    final, usage, model_id = triple(parsed)
    assert final == "ok"
    assert (usage.input_tokens, usage.output_tokens) == (7, 2)
    assert usage.cache_read_tokens == 5 and usage.cache_write_tokens is None
    assert model_id == "glm-4.7-flash:latest"
    # Without a usage file the stream's result event supplies the counts.
    _, stream_usage, _ = triple(parse_hermes(fixture("hermes_ok.jsonl"), None))
    assert stream_usage.input_tokens > 0
    assert (stream_usage.cache_read_tokens, stream_usage.cache_write_tokens) == (0, 0)


def test_parse_hermes_records_cache_read():
    tokens = {"input": 223, "output": 9, "cache_read": 3901, "cache_write": 0}
    result = {"type": "result", "exit_code": 0, "text": "ok", "tokens": tokens}
    _, usage, _ = triple(parse_hermes(json.dumps(result), None))
    assert (usage.input_tokens, usage.cache_read_tokens) == (223, 3901)
    assert usage.cache_write_tokens == 0


def test_run_no_usage_is_infra_error(tmp_path):
    runner = FakeRunner(fixture("claude_no_usage.txt"))
    driver = ClaudeCodeDriver(
        tmp_root=tmp_path, runner=runner, credentials=fake_claude_auth(tmp_path)
    )
    run = driver.run("hi", "claude-x", "http://mcp", "secret-token", 60)
    assert run.infra_error
    assert run.usage is None
    assert run.timed_out is False


def test_run_model_error_is_infra_error(tmp_path):
    runner = FakeRunner(fixture("hermes_model_error.jsonl"))
    driver = HermesDriver("http://ollama:11434", tmp_root=tmp_path, runner=runner)
    run = driver.run("hi", "no-such-model:1b", "http://mcp", "secret-token", 60)
    assert run.infra_error
    assert run.usage is None


def test_run_timeout(tmp_path):
    runner = FakeRunner(raise_timeout=True)
    run = codex_driver(tmp_path, runner).run(
        "hi", "gpt-x", "http://mcp", "secret-token", 5
    )
    assert run.timed_out is True
    assert run.infra_error is None
    assert run.usage is None


def test_claude_command_isolated(tmp_path):
    runner = FakeRunner(fixture("claude_ok.json"))
    driver = ClaudeCodeDriver(
        tmp_root=tmp_path, runner=runner, credentials=fake_claude_auth(tmp_path)
    )
    run = driver.run("hi", "claude-x", "http://mcp", "secret-token", 60)
    assert run.infra_error is None
    assert "--strict-mcp-config" in runner.argv
    assert "--tools" in runner.argv
    home = Path(runner.env["HOME"])
    assert home.parent == tmp_path
    assert ".claude/.credentials.json" in runner.home_files
    assert not home.exists()  # isolated home is deleted after the attempt
    assert runner.env["BENCH_TOKEN"] == "secret-token"
    assert not any("secret-token" in arg for arg in runner.argv)


def test_codex_and_hermes_keep_token_off_argv(tmp_path):
    codex = FakeRunner(fixture("codex_ok.jsonl"))
    codex_driver(tmp_path, codex).run("hi", "gpt-x", "http://mcp", "secret-token", 60)
    hermes = FakeRunner(fixture("hermes_ok.jsonl"))
    HermesDriver("http://ollama:11434", tmp_root=tmp_path, runner=hermes).run(
        "hi", "glm", "http://mcp", "secret-token", 60
    )
    for runner in (codex, hermes):
        assert runner.env["BENCH_TOKEN"] == "secret-token"
        assert not any("secret-token" in arg for arg in runner.argv)
    assert "shell_tool" in codex.argv
    assert "model_catalog.json" in codex.home_files
    assert hermes.argv[hermes.argv.index("-t") + 1] == "bench"


def test_ollama_models_parses_tags(monkeypatch):
    def fake_get(url, **kwargs):
        assert url == "http://ollama:11434/api/tags"
        request = httpx.Request("GET", url)
        return httpx.Response(
            200, json={"models": [{"name": "gpt-oss:120b"}]}, request=request
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert ollama_models("http://ollama:11434") == ["gpt-oss:120b"]


def test_codex_refuses_model_missing_from_catalog(tmp_path):
    runner = FakeRunner(fixture("codex_ok.jsonl"))
    run = codex_driver(tmp_path, runner).run("hi", "gpt-new", "http://mcp", "t", 60)
    assert run.infra_error and "gpt-new" in run.infra_error
    assert run.usage is None
    assert runner.argv == []  # codex never started with fallback tool metadata


def test_write_back_replaces_changed_login(tmp_path):
    original = tmp_path / "auth.json"
    original.write_text("old")
    original.chmod(0o640)
    copy = tmp_path / "copy.json"
    copy.write_text("rotated")
    write_back(copy, original, b"old")
    assert original.read_text() == "rotated"
    assert original.stat().st_mode & 0o777 == 0o640  # mode preserved
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "auth.json",
        "copy.json",
    ]


def test_write_back_leaves_unchanged_login(tmp_path):
    original = tmp_path / "auth.json"
    original.write_text("same")
    os.utime(original, (1_000_000, 1_000_000))
    copy = tmp_path / "copy.json"
    copy.write_text("same")
    write_back(copy, original, b"same")
    assert original.stat().st_mtime == 1_000_000


def test_write_back_keeps_login_refreshed_elsewhere(tmp_path):
    original = tmp_path / "auth.json"
    original.write_text("fresh from the user's own session")
    copy = tmp_path / "copy.json"
    copy.write_text("snapshot")  # client did not touch its copy
    write_back(copy, original, b"snapshot")
    assert original.read_text() == "fresh from the user's own session"


def test_failed_write_back_still_removes_temp_home(tmp_path, monkeypatch):
    def broken_write_back(*args):
        raise OSError("disk full")

    monkeypatch.setattr(claude_code, "write_back", broken_write_back)
    homes = tmp_path / "homes"
    homes.mkdir()
    driver = ClaudeCodeDriver(
        tmp_root=homes,
        runner=FakeRunner(fixture("claude_ok.json")),
        credentials=fake_claude_auth(tmp_path),
    )
    with pytest.raises(OSError):
        driver.run("hi", "claude-x", "http://mcp", "t", 60)
    assert list(homes.iterdir()) == []


def test_missing_login_or_cache_is_infra_error(tmp_path):
    missing = tmp_path / "nope.json"
    runs = [
        ClaudeCodeDriver(tmp_root=tmp_path, runner=FakeRunner(), credentials=missing),
        CodexDriver(
            tmp_root=tmp_path,
            runner=FakeRunner(),
            auth=missing,
            models_cache=fake_models_cache(tmp_path),
        ),
        CodexDriver(
            tmp_root=tmp_path,
            runner=FakeRunner(),
            auth=fake_claude_auth(tmp_path),
            models_cache=missing,
        ),
    ]
    for driver in runs:
        run = driver.run("hi", "gpt-x", "http://mcp", "t", 60)
        assert run.infra_error and run.usage is None
        assert driver.runner.argv == []
    assert [path.name for path in tmp_path.iterdir() if path.is_dir()] == []


def test_codex_empty_usage_is_none():
    for usage in ({}, None):
        event = {"type": "turn.completed"}
        if usage is not None:
            event["usage"] = usage
        assert parse_codex(json.dumps(event)).usage is None


def test_codex_model_id_falls_back_to_requested(tmp_path):
    runner = FakeRunner(fixture("codex_ok.jsonl"))
    run = codex_driver(tmp_path, runner).run("hi", "gpt-x", "http://mcp", "t", 60)
    assert run.model_id == "gpt-x"


def test_driver_writes_back_rotated_claude_login(tmp_path):
    credentials = fake_claude_auth(tmp_path)

    class RotatingRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            rotated = Path(kwargs["cwd"]) / ".claude" / ".credentials.json"
            rotated.write_text('{"rotated": true}')
            return super().__call__(argv, **kwargs)

    runner = RotatingRunner(fixture("claude_ok.json"))
    ClaudeCodeDriver(tmp_root=tmp_path, runner=runner, credentials=credentials).run(
        "hi", "claude-x", "http://mcp", "t", 60
    )
    assert credentials.read_text() == '{"rotated": true}'


def test_run_in_group_kills_whole_group_on_timeout():
    argv = ["sh", "-c", "echo $$; sleep 60 & sleep 60"]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_in_group(argv, capture_output=True, text=True, timeout=1)
    group = int(caught.value.stdout.split()[0])
    time.sleep(0.2)
    assert live_group_members(group) == []


def live_group_members(group: int) -> list[int]:
    """Return non-zombie pids whose process group is ``group`` (Linux /proc)."""
    members = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[2]) == group and fields[0] != "Z":
            members.append(int(stat.parent.name))
    return members


def test_hermes_turn_cap_is_model_failure_not_infra(tmp_path):
    """Captured live: glm-4.7-flash with --max-turns 1 (exit 1, no error)."""
    runner = FakeRunner(fixture("hermes_max_turns.jsonl"), returncode=1)
    driver = HermesDriver("http://ollama:11434", tmp_root=tmp_path, runner=runner)
    run = driver.run("hi", "glm-4.7-flash:latest", "http://mcp", "t", 60)
    assert run.infra_error is None
    assert run.model_failure and "turn cap" in run.model_failure
    assert run.usage.input_tokens == 3491
    assert runner.argv[runner.argv.index("--max-turns") + 1] == "30"


def test_exit_zero_with_usage_and_empty_final_is_judged(tmp_path):
    turn = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}
    run = codex_driver(tmp_path, FakeRunner(json.dumps(turn))).run(
        "hi", "gpt-x", "http://mcp", "t", 60
    )
    assert run.final_message == "" and run.usage is not None
    assert run.infra_error is None and run.model_failure is None


def test_nonzero_exit_with_usage_is_still_infra(tmp_path):
    turn = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}
    run = codex_driver(tmp_path, FakeRunner(json.dumps(turn), returncode=2)).run(
        "hi", "gpt-x", "http://mcp", "t", 60
    )
    assert run.infra_error and run.model_failure is None


def test_raw_usage_kept_with_token_safe_keys(tmp_path):
    claude = ClaudeCodeDriver(
        tmp_root=tmp_path,
        runner=FakeRunner(fixture("claude_ok.json")),
        credentials=fake_claude_auth(tmp_path),
    ).run("hi", "claude-opus-5-5", "http://mcp", "t", 60)
    assert claude.raw_usage["total_cost_usd"] > 0
    assert claude.raw_usage["num_turns"] == 1
    assert claude.raw_usage["usage"]["input_tok"] == 2
    turn = {"type": "turn.completed",
            "usage": {"input_tokens": 9, "reasoning_output_tokens": 4}}  # fmt: skip
    codex = codex_driver(tmp_path, FakeRunner(json.dumps(turn))).run(
        "hi", "gpt-x", "http://mcp", "t", 60
    )
    assert codex.raw_usage == {"turns": 1, "input_tok": 9, "reasoning_output_tok": 4}
    hermes_runner = FakeRunner(fixture("hermes_ok.jsonl"))
    hermes = HermesDriver("http://o:11434", tmp_root=tmp_path, runner=hermes_runner)
    hermes = hermes.run("hi", "glm", "http://mcp", "t", 60)
    assert hermes.raw_usage["tok"]["input"] == 13427
    for raw in (claude.raw_usage, codex.raw_usage, hermes.raw_usage):
        assert "tokens" not in json.dumps(raw).lower()


def test_claude_model_id_matches_requested_and_usage_sums_models():
    result = json.loads(fixture("claude_ok.json"))
    main = result["modelUsage"]["claude-opus-5-5"]
    helper = {"inputTokens": 10, "outputTokens": 3, "cacheReadInputTokens": 0,
              "cacheCreationInputTokens": 7}  # fmt: skip
    result["modelUsage"] = {"claude-haiku-5": helper, "claude-opus-5-5": main}
    parsed = parse_claude(json.dumps(result), "claude-opus-5-5")
    assert parsed.model_id == "claude-opus-5-5"
    assert parsed.usage.input_tokens == main["inputTokens"] + 10
    assert parsed.usage.output_tokens == main["outputTokens"] + 3
    assert parsed.usage.cache_write_tokens == main["cacheCreationInputTokens"] + 7
    assert parse_claude(json.dumps(result), "opus").model_id == "claude-opus-5-5"
    assert parse_claude(json.dumps(result), "sonnet").model_id == "claude-haiku-5"


def test_hermes_warm_up(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, body=json)
        if json["model"] == "missing":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    driver = HermesDriver("http://o:11434/")
    assert driver.warm_up("glm") is None
    assert sent["url"] == "http://o:11434/api/generate"
    assert sent["body"]["options"] == {"num_predict": 1}
    assert sent["body"]["prompt"] == "ok" and sent["body"]["keep_alive"]
    assert "warm-up of missing failed" in driver.warm_up("missing")


def test_run_in_group_kills_group_on_keyboard_interrupt(monkeypatch):
    started = {}
    real_communicate = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        if self.pid not in started:
            started[self.pid] = True
            time.sleep(0.2)
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_in_group(["sh", "-c", "sleep 60 & sleep 60"], capture_output=True)
    [group] = started
    time.sleep(0.2)
    assert live_group_members(group) == []
