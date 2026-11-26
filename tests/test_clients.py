"""Tests for the headless client drivers (fixtures captured from real CLIs)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from mechubbench.clients.claude_code import ClaudeCodeDriver, parse_claude
from mechubbench.clients.codex import CodexDriver, isolated_catalog, parse_codex
from mechubbench.clients.hermes import HermesDriver, ollama_models, parse_hermes

FIXTURES = Path(__file__).parent / "fixtures" / "clients"


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
    final, usage, model_id = parse_claude(fixture("claude_ok.json"))
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
    final, usage, _ = parse_codex(stdout)
    assert final == "hi"
    assert (usage.input_tokens, usage.output_tokens) == (20, 6)
    assert usage.cache_read_tokens == 8
    assert usage.cache_write_tokens is None


def test_parse_codex_fixture():
    final, usage, _ = parse_codex(fixture("codex_ok.jsonl"))
    assert final == "ok"
    assert usage.input_tokens > 0


def test_parse_hermes_ok(tmp_path):
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({"input_tokens": 7, "output_tokens": 2}))
    final, usage, model_id = parse_hermes(fixture("hermes_ok.jsonl"), usage_file)
    assert final == "ok"
    assert (usage.input_tokens, usage.output_tokens) == (7, 2)
    assert usage.cache_read_tokens is None and usage.cache_write_tokens is None
    assert model_id == "glm-4.7-flash:latest"
    # Without a usage file the stream's result event supplies the counts.
    _, stream_usage, _ = parse_hermes(fixture("hermes_ok.jsonl"), None)
    assert stream_usage.input_tokens > 0


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
