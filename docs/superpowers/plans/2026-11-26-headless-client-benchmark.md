# Headless-client Junos benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run six Junos tasks through Claude Code, Codex and Hermes (Ollama) against a bench-only rust-junosmcp, judge each attempt by script, and publish a static HTML chart of tokens and speed with pass/fail, per model and per test, with a before/after comparison.

**Architecture:** A new `bench matrix` command fans each scenario out over the bench devices whose platform matches, runs each client headless in an isolated home with only the benchmark MCP server, retries up to three attempts, restores the device after every attempt, and writes a schema-v2 manifest. `bench report` turns one or two manifests into a self-contained HTML page. The existing direct-API runner (`runner.py`, `bench run`) is untouched.

**Tech Stack:** Python ≥3.11, uv, pytest, ruff (line length 88, rules E,F,W,I), pyyaml, jsonschema, httpx (no new runtime deps). Chart is hand-built inline SVG.

**Spec:** `docs/superpowers/specs/2026-11-26-headless-client-benchmark-design.md`

## Global Constraints

- Only `bench-*` devices, via a separate benchmark rust-junosmcp instance on VMID 611 `test-labmode-junos` (pve2, tagged `disposable;labmode;test`). Never prod-labmode-junos; never a `protected` guest.
- Every client runs with only the benchmark MCP server and all built-in tools off (no shell, file edit, web), in an isolated config/HOME.
- Clients: Claude Code and Codex on each vendor's latest flagship; Hermes on every Ollama model on strix at run time. No Copilot driver (keep the driver interface open for it).
- Up to three attempts per model × test × device; stop at first pass. Three fails = full failure, its numbers not charted. Pass after fails charts the passing attempt plus the fail count.
- `infra_error` (client crash, Ollama OOM, MCP-server error, device unreachable) does not use an attempt and is re-run; a timeout counts as a failed attempt.
- Wall time = client launch to client exit only.
- Chart total tokens (input + output) on one shared axis; cache read and cache write as separate columns, blank when the client doesn't report them; a page note says clients count slightly differently.
- Full failures are a marked cell and are excluded from every average.
- Report flags an Ollama model that is a full failure on more than one test; nothing is dropped automatically.
- Runs are serial and resumable from the manifest.
- Each run first checks every bench device against its golden config fingerprint and refuses to start on drift. After every write test, restore to the pre-test baseline unconditionally, then run the fingerprint check; on failure the run stops.
- Prompts name hosts and devices with IPs (Host A 10.66.151.10, Host B 10.66.162.10, Host C 10.66.171.10).
- Server version comes from the benchmark instance; exact client model id recorded per result.
- No secrets in repo, results or HTML: tokens stay in gitignored 0600 files; everything written passes `redact.redact_manifest`.
- Tests: `uv run --extra dev pytest -q`; lint: `uv run --extra dev ruff check . && uv run --extra dev ruff format --check .`.

## Review Focus

1. A client that exits 0 but prints no usage block (e.g. auth expired, rate limit text) → must be `infra_error`, not a pass/fail with zero tokens. Test in Task 3.
2. A model does a plain `commit` (no confirm) or leaves a commit-confirmed pending when the client exits → restore still returns the device to baseline and the attempt fails. Test in Task 5.
3. A run killed mid-attempt and resumed → the half-finished attempt is re-run, completed results are not, and attempt numbering stays correct. Test in Task 6.
4. An Ollama model that is not tool-capable or not loaded → Hermes error surfaces as `infra_error`, retried, and after the infra retry cap (3) recorded as `infra_error` verdict, not full failure. Test in Task 3 and Task 6.
5. Report given two manifests with different model sets or tests → unmatched cells show "—" in the other column, never a crash or a misaligned row. Test in Task 7.

---

## File Structure

| Path | Responsibility |
|---|---|
| `bench/devices.yaml` (create) | Bench inventory: name, platform, release, `mgmt_instance`, IPs of hosts |
| `bench/clients.yaml` (create) | Client → model ids, timeouts |
| `mechubbench/inventory.py` (create) | Load `bench/devices.yaml`; `devices_for(scenario)` fan-out |
| `mechubbench/clients/__init__.py`, `base.py` (create) | `AttemptUsage`, `ClientRun`, `Driver` protocol, isolated-home helper |
| `mechubbench/clients/claude_code.py`, `codex.py`, `hermes.py` (create) | One headless driver each |
| `mechubbench/judge.py` (create) | Post-attempt checks: answer keywords, device-state commands, commit-confirmed |
| `mechubbench/guard.py` (create) | Golden drift check, per-attempt baseline capture and unconditional restore |
| `mechubbench/matrix.py` (create) | Orchestrator: fan-out, attempts loop, resume, manifest v2 |
| `mechubbench/report.py` (create) | Manifest(s) → self-contained HTML |
| `mechubbench/cli.py` (modify, `build_parser` :436) | Add `matrix` and `report` subcommands |
| `scenarios/schema.json` (modify) | Add `platforms`, `requires`, `device_checks`, `commit_confirmed_required`, `fault` |
| `scenarios/bench/*.yaml` (create) | The six tests |
| `tests/test_inventory.py`, `test_clients.py`, `test_judge.py`, `test_guard.py`, `test_matrix.py`, `test_report.py` (create) | Unit tests with fakes |
| `tests/fixtures/clients/*` (create) | Captured real client outputs |
| `README.md` (modify) | `bench matrix` / `bench report` usage |

Shared types (defined in Task 2, used everywhere):

```python
@dataclass
class AttemptUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None

@dataclass
class ClientRun:
    exit_code: int
    final_message: str
    wall_seconds: float
    usage: AttemptUsage | None      # None = client reported no usage
    model_id: str | None            # exact id the client reports
    infra_error: str | None         # set when the run failed for non-model reasons
    timed_out: bool
```

Outcome strings: `"pass" | "fail" | "timeout" | "infra_error"`. Verdict strings: `"pass" | "full_failure" | "infra_error"`.

---

### Task 1: Benchmark rust-junosmcp instance on VMID 611

Infra, no repo code. Every step needs the guardrail evidence printed.

**Files:** none in repo. Record in strix `~/homelab/ipam/homelab.md` and `~/homelab/runbooks/2026-11-24-junos-bench-lab.md` (§ Operating notes).

- [ ] **Step 1: Resolve 611.** `ssh root@pve3.mechub.org "pvesh get /cluster/resources --type vm --output-format json"` → confirm vmid 611 name `test-labmode-junos`, node pve2, tags include `disposable`, not `protected`. Print `qm config 611` or `pct config 611` (on pve2) before starting it.
- [ ] **Step 2: Start it and record its address** from the IPAM registry (existing row if any; otherwise pick per `~/homelab/ipam/README.md`).
- [ ] **Step 3: Install the current released rust-junosmcp** (same version as prod-labmode-junos on LXC 950: `/usr/local/bin/rust-junosmcp --version` there). Record that version.
- [ ] **Step 4: Inventory** `/etc/jmcp/devices.json` with only the six `bench-*` devices (copy those entries and their known_hosts lines from LXC 950; ssh key auth as there). `reload_devices`.
- [ ] **Step 5: Two tokens.** Agent token: read + change-set + commit scopes, device scope `bench-*`. Operator token (setup/restore/judge): same device scope plus rollback. Save on ArmyGreen as `~/Projects/mechubbench/.env.bench-token` and `.env.bench-setup-token`, mode 0600; add both to `.gitignore`.
- [ ] **Step 6: Verify.** With the agent token: `get_router_list` returns exactly the six `bench-*` names; `gather_device_facts` works on `bench-srx345`; a call naming a non-bench device is refused. Expected: 6 names, facts OK, refusal.
- [ ] **Step 7: Record** the endpoint URL, version and token file names (not values) in the runbook's operating notes; IPAM row updated.

### Task 2: Inventory, shared types and scenario schema

**Files:**
- Create: `bench/devices.yaml`, `bench/clients.yaml`, `mechubbench/inventory.py`, `mechubbench/clients/__init__.py`, `mechubbench/clients/base.py`
- Modify: `scenarios/schema.json`
- Test: `tests/test_inventory.py`

**Interfaces:**
- Produces: `AttemptUsage`, `ClientRun` (above); `BenchDevice(name: str, platform: str, release: str, mgmt_instance: bool)`; `load_inventory(path: Path) -> list[BenchDevice]`; `devices_for(scenario: dict, inventory: list[BenchDevice]) -> list[BenchDevice | None]` (a `None` entry = one multi-device record, see `device_scope: path`); `isolated_home(tmp_root: Path) -> Path`; `Driver` protocol `run(prompt: str, model: str, mcp_url: str, token: str, timeout_s: int) -> ClientRun`; `load_clients(path) -> dict[str, dict]`.

`bench/devices.yaml` values (from the runbook): bench-ex2300 `ex` 23.4R2-S2.1 false; bench-srx1600 `srx-classic` 25.4R1.12 true; bench-srx440 `srx-evo` 25.4X300-D40.2-EVO true; bench-srx345 `srx-classic` 25.4R1-S2.3 false; bench-vsrx-inet `vsrx` 26.2R1.7 true; bench-csrx `csrx` 26.2R1.7 false. Plus `hosts: {A: 10.66.151.10, B: 10.66.162.10, C: 10.66.171.10}`.

`bench/clients.yaml`: `claude-code: {model: claude-opus-5-5, timeout_s: 900}`, `codex: {model: gpt-6.2-astra, timeout_s: 900}`, `hermes: {models: ollama, ollama_url: http://strix:11434, timeout_s: 1800}`. Model ids confirmed by the user 2026-11-26.

Schema additions (all optional): `platforms: [ex|srx-classic|srx-evo|vsrx|csrx]`, `requires: [mgmt_instance]`, `device_scope: per_device|path` (default per_device), `devices: [name]` (explicit subset, intersected with platforms), `fault: <set lines>` (planted by operator before the attempt, removed by restore), `device_checks: [{command, must_contain?: [..], must_match?: regex, must_not_contain?: [..]}]`, `commit_confirmed_required: bool`. Keep `expected_calls`/`forbidden_calls` required by allowing empty lists.

- [ ] **Step 1: Write failing tests** in `tests/test_inventory.py`:
  - `test_devices_for_platform_filter`: scenario `platforms: [srx-classic, srx-evo, vsrx, csrx]` → names `{bench-srx1600, bench-srx440, bench-srx345, bench-vsrx-inet, bench-csrx}`.
  - `test_devices_for_requires_mgmt_instance`: `platforms` all SRX + `requires: [mgmt_instance]` → `{bench-srx1600, bench-srx440, bench-vsrx-inet}`.
  - `test_devices_for_path_scope`: `device_scope: path` → `[None]`.
  - `test_devices_for_unknown_platform_raises`: `platforms: [mx]` → `ValueError`.
  - `test_isolated_home_has_no_user_config`: returned dir exists, is empty, and is under `tmp_root`.
  - `test_bench_scenarios_validate`: every `scenarios/bench/*.yaml` passes `core.validate_scenario` (skip if dir empty).
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/test_inventory.py -q` → FAIL (import error).
- [ ] **Step 3: Implement** the files above.
- [ ] **Step 4: Run** the same → PASS; `uv run --extra dev pytest -q` → all pass (existing 94+ still green).
- [ ] **Step 5: Commit** `feat: bench inventory, client types and scenario schema fields`.

### Task 3: Headless client drivers

**Files:**
- Create: `mechubbench/clients/claude_code.py`, `codex.py`, `hermes.py`, `tests/fixtures/clients/{claude_ok.json,claude_no_usage.txt,codex_ok.jsonl,hermes_ok.jsonl,hermes_model_error.jsonl}`
- Test: `tests/test_clients.py`

**Interfaces:**
- Consumes: `ClientRun`, `AttemptUsage`, `isolated_home` (Task 2).
- Produces: `ClaudeCodeDriver`, `CodexDriver`, `HermesDriver`, each implementing `Driver.run`; module functions `parse_claude(stdout: str) -> tuple[str, AttemptUsage | None, str | None]`, `parse_codex(stdout: str) -> ...`, `parse_hermes(stdout: str, usage_file: Path | None) -> ...` returning (final_message, usage, model_id); `ollama_models(url: str) -> list[str]` (GET `/api/tags`).

Invocation (verify each flag against `--help` of the installed version; Claude Code 2.1.304, codex-cli 0.163.0, Hermes 0.22.x/0.23.x):
- Claude Code: `claude -p <prompt> --output-format json --model <m> --strict-mcp-config --mcp-config <home>/mcp.json --tools "" --setting-sources "" --permission-mode bypassPermissions --allowedTools "mcp__bench__*"`, `HOME=<isolated>` plus the auth the CLI needs copied into the isolated home (credentials file copied 0600, never logged). Usage from the result JSON's `usage` (input, output, cache_read_input_tokens, cache_creation_input_tokens); model id from `modelUsage` keys.
- Codex: `codex exec --json --ephemeral --skip-git-repo-check -m <m> -s read-only -c 'mcp_servers.bench.url="<url>"' -c 'mcp_servers.bench.bearer_token_env_var="BENCH_TOKEN"'` with `CODEX_HOME=<isolated>` (auth copied). Usage summed over `turn.completed` events (`input_tokens`, `cached_input_tokens` → cache_read, `output_tokens`; cache_write None). Disable its shell tool by config (find the key in `codex exec --help`/docs); if it cannot be fully disabled, fail Step 6.
- Hermes: `hermes chat -q <prompt> --oneshot --format stream-json --ignore-user-config --ignore-rules -m <ollama model> --provider <ollama provider> --usage-file <home>/usage.json --max-turns 30 -t <mcp toolset only>` with Hermes home pointed at the isolated dir and a config there containing only the bench MCP server and the Ollama provider (`ollama_url`). Cache columns None.

Classification inside `run`: non-zero exit, no parseable final result, or `usage is None` → `infra_error` set (message = first stderr/stdout line, redacted). Timeout (`subprocess.run(..., timeout=timeout_s)` raises) → `timed_out=True`, process killed, `usage` whatever was parsed (may be None). Wall time = `time.monotonic()` around the subprocess only.

- [ ] **Step 1: Capture fixtures.** Run each client once with prompt `"Reply with the word ok."`, no MCP, and save stdout to the fixture files; strip any account ids/emails by hand and check with `grep -iE 'token|key|@'`. For `claude_no_usage.txt` and `hermes_model_error.jsonl` save a real failure (bad model name).
- [ ] **Step 2: Write failing tests** in `tests/test_clients.py`:
  - `test_parse_claude_ok`: final message contains `ok`; usage input/output > 0; cache fields are ints; model_id non-empty.
  - `test_parse_codex_sums_turns`: synthetic two-`turn.completed` JSONL → input/output summed; `cache_write_tokens is None`.
  - `test_parse_hermes_ok`: usage from usage file; cache fields None.
  - `test_run_no_usage_is_infra_error` (Review Focus 1): fake subprocess returns exit 0 + `claude_no_usage.txt` → `ClientRun.infra_error` set, `usage is None`.
  - `test_run_model_error_is_infra_error` (Review Focus 4): Hermes fixture `hermes_model_error.jsonl` → infra_error set.
  - `test_run_timeout`: fake subprocess raises `TimeoutExpired` → `timed_out is True`, `infra_error is None`.
  - `test_claude_command_isolated`: built argv contains `--strict-mcp-config` and `--tools`; env `HOME` equals the isolated dir.
  - `test_ollama_models_parses_tags`: httpx mock returns `{"models":[{"name":"gpt-oss:120b"}]}` → `["gpt-oss:120b"]`.
- [ ] **Step 3: Run** `uv run --extra dev pytest tests/test_clients.py -q` → FAIL.
- [ ] **Step 4: Implement** the three drivers (subprocess injected via a `runner` callable parameter defaulting to `subprocess.run`, so tests fake it).
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Live isolation probe** (against the Task 1 instance, agent token): for each client, prompt `"Run the shell command id and tell me its output. Then list your tools."`. Expected: no `uid=` in the answer, and only `bench` MCP tools listed. Any shell output = stop and fix before continuing.
- [ ] **Step 7: Commit** `feat: headless drivers for Claude Code, Codex and Hermes`.

### Task 4: Judge

**Files:**
- Create: `mechubbench/judge.py`
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: `runner.MCPClient` (operator token), scenario dict.
- Produces: `judge_attempt(scenario: dict, device: str | None, final_message: str, mcp: MCPClient, commits_since_baseline: list[str]) -> tuple[bool, str]` (pass, reason). Reuses `scoring.score_scenario(scenario, transcript=[], final_message=...)` for `outcome.report_contains`; runs each `device_checks[].command` via `execute_junos_command` on `device` (for `device_scope: path`, the check names its own `device` key) and applies `must_contain` / `must_match` / `must_not_contain`; when `commit_confirmed_required`, passes only if some entry in `commits_since_baseline` contains `commit confirmed`.

- [ ] **Step 1: Write failing tests** (fake MCP returning canned text):
  - `test_report_keywords_required`: missing keyword → `(False, reason mentions keyword)`.
  - `test_device_check_must_contain`: check output lacks string → fail; contains → pass.
  - `test_commit_confirmed_required_plain_commit_fails` (Review Focus 2): commits `["0 2026-11-26 ... by netconf via netconf"]` → fail, reason `no commit confirmed`.
  - `test_commit_confirmed_required_ok`: list containing `commit confirmed, rollback in 10mins` → pass.
  - `test_empty_final_message_fails` when `report_contains` set.
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/test_judge.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: scripted judge for answers, device state and commit confirmed`.

### Task 5: Guard — drift check, fault, baseline, restore

**Files:**
- Create: `mechubbench/guard.py`
- Test: `tests/test_guard.py`

**Interfaces:**
- Consumes: `runner.fetch_config_fingerprint`, `runner.verify_baseline_restored`, `runner.prepare_setup_config` (existing), operator `MCPClient`.
- Produces:
  - `check_golden(mcp, inventory, golden: dict[str, str]) -> list[str]` — names that drifted (empty = OK). `golden` is stored in `bench/golden-fingerprints.json`, written by `record_golden(mcp, inventory, path)`.
  - `Baseline(device: str, fingerprint: str, top_commit: str)`; `capture_baseline(mcp, device) -> Baseline` (fingerprint + entry 0 of `show system commit`).
  - `plant_fault(mcp, device, fault: str) -> None` via `load_and_commit_config` (as `run_all_scenarios_agentic` does today).
  - `commits_since(mcp, baseline) -> list[str]` — `show system commit` entries above `baseline.top_commit`.
  - `restore(mcp, baseline) -> None`: if a commit confirmed is pending, confirm it first (`commit` via the change-set tool or CLI the server allows, so the window cannot auto-revert mid-restore); then `rollback_config(version=len(commits_since))` and commit; then `junos_config_diff` version 0 empty and `verify_baseline_restored`. Raises `RuntimeError` on any failure. A no-op when `commits_since` is empty (read-only tests) except the fingerprint check.

- [ ] **Step 1: Write failing tests** (fake MCP that tracks a commit list and fingerprint):
  - `test_restore_after_plain_commit` (Review Focus 2): one plain commit since baseline → rollback version 1 issued, fingerprint back, no raise.
  - `test_restore_with_pending_confirmed`: pending `commit confirmed` + nothing else → confirm then rollback 1.
  - `test_restore_two_commits`: two commits → rollback version 2.
  - `test_restore_readonly_noop`: no commits → no rollback call, fingerprint checked.
  - `test_restore_fingerprint_mismatch_raises`.
  - `test_check_golden_reports_drift`: one device fingerprint differs → `["bench-srx345"]`.
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/test_guard.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: golden drift check and unconditional restore to baseline`.

### Task 6: Matrix orchestrator and manifest v2

**Files:**
- Create: `mechubbench/matrix.py`
- Modify: `mechubbench/cli.py` (add `matrix` subparser in `build_parser` :436; reuse `_read_token` :48)
- Test: `tests/test_matrix.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: `run_matrix(scenarios: list[dict], entries: list[tuple[str, str, Driver]], inventory, agent_mcp_url: str, agent_token: str, operator_mcp: MCPClient, manifest_path: Path, server_version: str, golden: dict) -> dict` where `entries` = (client name, model, driver); CLI `bench matrix --scenarios scenarios/bench --inventory bench/devices.yaml --clients-config bench/clients.yaml [--clients claude-code,codex,hermes] --mcp-endpoint URL --mcp-token-file .env.bench-token --setup-token-file .env.bench-setup-token --out results/<run>.json [--resume]`.

Manifest v2 (written after every attempt, redacted with `redact.redact_manifest`, atomic write via temp file + rename):

```json
{"schema_version": 2, "run_id": "...", "server_version": "...", "corpus_git_sha": "...",
 "started": "...", "finished": null,
 "results": [{"client": "codex", "model": "...", "model_id": "...", "test": "<scenario id>",
   "device": "bench-srx440" | null, "platform": "srx-evo" | "path",
   "attempts": [{"n": 1, "outcome": "fail", "reason": "...", "wall_s": 41.2,
                 "tokens": {"input": 0, "output": 0, "cache_read": null, "cache_write": null}}],
   "infra_errors": 0, "verdict": "pass" | "full_failure" | "infra_error" | null,
   "fails_before_pass": 0}]}
```

Loop order: for each entry, each scenario, each device from `devices_for`. Per cell: up to 3 counted attempts; `infra_error` attempts are recorded with outcome `infra_error`, not counted, retried, capped at 3 infra errors → verdict `infra_error`. Per attempt: `capture_baseline` → `plant_fault` if scenario has one → `driver.run(render(prompt))` → `commits_since` → `judge_attempt` (skipped for infra/timeout; timeout = outcome `timeout`, counts as fail) → `restore` (always, in `finally`). `restore` raising → write manifest and exit non-zero. Before the first attempt: `check_golden` must be empty, else exit non-zero listing drifted devices. Prompt rendering replaces `{{device}}`, `{{host_a}}`, `{{host_b}}`, `{{host_c}}` from inventory. Hermes entries expand to one per `ollama_models(url)`.

Resume: load existing manifest; skip cells with a verdict; a cell without a verdict keeps its completed attempts and continues numbering from `len(counted attempts) + 1`.

- [ ] **Step 1: Write failing tests** (fake driver scripted with outcomes, fake guard/judge):
  - `test_pass_first_try`: verdict pass, 1 attempt, fails_before_pass 0.
  - `test_pass_after_two_fails`: fail, fail, pass → verdict pass, fails_before_pass 2, 3 attempts.
  - `test_three_fails_full_failure`: verdict full_failure, no 4th call.
  - `test_infra_error_not_counted`: infra, fail, infra, pass → 4 driver calls, verdict pass, fails_before_pass 1.
  - `test_infra_cap` (Review Focus 4): infra ×3 → verdict infra_error.
  - `test_timeout_counts_as_fail`.
  - `test_restore_runs_after_every_attempt` incl. when the driver raises.
  - `test_restore_failure_stops_run`: second restore raises → manifest written, exception propagates, no further driver calls.
  - `test_drift_refuses_start`: check_golden returns a name → no driver call, raises.
  - `test_resume_continues_numbering` (Review Focus 3): manifest with one cell verdict pass and one cell with attempts [fail] → only the second cell runs, its next attempt is `n: 2`.
  - `test_manifest_redacted_and_has_no_token`: fake token string never appears in the written file.
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/test_matrix.py -q` → FAIL.
- [ ] **Step 3: Implement** `run_matrix` and the CLI subcommand.
- [ ] **Step 4: Run** → PASS; full suite green; ruff clean.
- [ ] **Step 5: Commit** `feat: bench matrix orchestrator with retries, restore and resume`.

### Task 7: Static HTML report with before/after

**Files:**
- Create: `mechubbench/report.py`
- Modify: `mechubbench/cli.py` (`report` subparser)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: manifest v2.
- Produces: `render_report(run: dict, baseline: dict | None = None) -> str`; CLI `bench report --run results/B.json [--baseline results/A.json] --out results/report.html`.

Page content (one file, inline CSS and SVG, no network, light/dark via `prefers-color-scheme`):
- Header: run ids, server version(s), date, client/model list.
- Per test: grouped horizontal bars per model (client · model) of total tokens and of wall seconds (two small charts side by side, shared axes across models). Each bar labelled `pass`, `pass after N fails`, or the cell shown as a hatched marker `full failure` with no bar. Device breakdown in a `<details>` table per test: device, platform, verdict, tokens (input, output, cache read, cache write; blank when null), wall s.
- Summary table: model × test verdict grid with pass rate over devices (e.g. `4/5`).
- Averages per model exclude full failures and infra errors.
- When `baseline` given: each bar pair shows baseline vs this run; summary grid shows both; cells present in only one run show `—`.
- Flag box: Ollama (Hermes) models with full failures on more than one test.
- Footnote: "Clients count tokens slightly differently; cache columns are blank where a client does not report them."

- [ ] **Step 1: Write failing tests:**
  - `test_full_failure_has_no_bar`: a full_failure cell renders the marker text `full failure` and no `<rect` with that cell's data id.
  - `test_pass_after_fails_label`: label `pass after 2 fails`.
  - `test_null_cache_renders_blank`: `cache_read: null` → empty `<td></td>`, never `None` or `0`.
  - `test_average_excludes_full_failure`.
  - `test_ollama_flag`: hermes model with two full-failure tests appears in the flag box; one with one does not.
  - `test_compare_mismatched_runs` (Review Focus 5): baseline lacks a model → `—` cells, no exception.
  - `test_no_external_urls`: assert neither `http://` nor `https://` appears in the output.
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/test_report.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Render a fixture manifest, open `results/report.html` in a browser (`/browse`), check both themes and a 390 px wide viewport.
- [ ] **Step 5: Commit** `feat: static HTML report with before/after comparison`.

### Task 8: The six scenarios and the baseline run

**Files:**
- Create: `scenarios/bench/{read-explain,ex-mac-port,srx-443-troubleshoot,safe-change-8080,version-trap-dns,flow-trace-a-c}.yaml`, `bench/golden-fingerprints.json`
- Modify: `README.md`

Expected values come from `~/homelab/topology/bench-lab/final-configs/*.set` and the runbook, plus read-only checks with the operator token. Do not invent values.

| File | platforms / scope | Key fields |
|---|---|---|
| read-explain | all SRX, per_device | `report_contains`: zone names, `permit-lab`, `deny-all`, that device's IKE gateway peer address, `source nat` (cSRX: its zones and policies; no IPsec) |
| ex-mac-port | `devices: [bench-ex2300]` | `report_contains`: the port and VLAN of Host A's lab MAC (read the MAC on Host A's lab NIC and the EX `show ethernet-switching table` once while writing the fixture) |
| srx-443-troubleshoot | `devices: [bench-srx440]`, `fault`: remove `junos-https` from `permit-lab` trust→vpn | `report_contains`: `bench-srx440`, `permit-lab`, `https`. The prompt says diagnose only; the fault is removed by `restore` whatever the model did. |
| safe-change-8080 | `devices: [bench-srx440, bench-srx1600, bench-csrx]` | `commit_confirmed_required: true`; `device_checks`: `show configuration security policies | display set` must_match a rule permitting tcp 8080 for 10.66.151.10 → 10.66.162.10 |
| version-trap-dns | all SRX, `requires: [mgmt_instance]` | prompt "set DNS servers 1.1.1.2 and 1.0.0.2 on {{device}}" with the existing name-server lines removed by `fault`; `device_checks`: `show configuration system name-server | display set` must_contain `routing-instance mgmt_junos`; `show host www.juniper.net` must_match an IPv4 address |
| flow-trace-a-c | `device_scope: path` | `report_contains`: `bench-srx440`, `bench-srx1600`, `bench-srx345`, `hairpin-lab`, `10.66.250.1`, `10.66.250.6`, zone names along the path |

- [x] **Step 1: Model ids** confirmed by the user: Claude Code `claude-opus-5-5`, Codex `gpt-6.2-astra` (user, 2026-11-26; first said "apstra-6.2").
- [ ] **Step 2: Write the six YAMLs**; `uv run --extra dev bench lint` and `pytest tests/test_inventory.py::test_bench_scenarios_validate` → PASS.
- [ ] **Step 3: Dry-check each scenario's judge on the live lab without a model**: for each, run `judge_attempt` with a hand-written correct answer (and, for write tests, a hand-made commit-confirmed change applied via the operator token, then `restore`). Expected: pass; then a wrong answer → fail. Proves every check can return both results.
- [ ] **Step 4: Record golden fingerprints** `bench matrix --record-golden` (add this flag in this task: calls `record_golden`) → `bench/golden-fingerprints.json` (fingerprints only, no config).
- [ ] **Step 5: Smoke run**: `bench matrix --clients claude-code --scenarios scenarios/bench/ex-mac-port.yaml ...` → one cell, verdict present, device unchanged (`check_golden` empty).
- [ ] **Step 6: Baseline run** of all clients and models; `bench report --run results/<baseline>.json --out results/report.html`. Expected: manifest has a verdict on every cell; `check_golden` empty at the end.
- [ ] **Step 7: README** section for `matrix`, `report`, token files, Task 1 instance.
- [ ] **Step 8: Commit** scenarios, golden file, README, `results/<baseline>.json` and `results/report.html` after `grep -iE 'bearer|token\"?:|password|pre-shared'` on the results finds nothing: `feat: six Junos bench scenarios and baseline run`.
