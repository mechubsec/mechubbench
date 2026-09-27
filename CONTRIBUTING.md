# Contributing to mechubbench

Thanks for considering a contribution. mechubbench is the benchmark harness the [mechub](https://github.com/fastrevmd-lab) family of open-source, self-hosted network-security tooling uses to show before/after performance and accuracy numbers instead of relying on hunches. It scores LLM tool-calling accuracy on firewall-automation tasks (Junos/PAN-OS) against a corpus of YAML scenarios. See [README.md](README.md) for the full picture.

## Before you start

- Check open issues and PRs first — someone may already be working on it.
- For anything larger than a small fix (a new scoring mode, a new runner mode, corpus restructuring), open an issue to discuss the approach first.
- This project follows one hard rule across the whole mechub fleet: **deterministic code decides, a model may only explain or draft.** The scoring logic in `mechubbench/scoring.py` is deterministic string/schema matching against a recorded transcript — it must never ask a model to grade its own run. Nothing you contribute should let a model's output decide whether a scenario passed, or push a change to a real device outside of a benchmark's own gated setup/teardown.

## Project layout

- `mechubbench/` — the package: `cli.py` (the `bench` command), `core.py` (pure, unit-testable scenario/tool loading and validation), `runner.py` (blind and agentic execution against an OpenAI-compatible endpoint, plus the MCP client used in agentic mode), `scoring.py` (deterministic pass/fail scoring)
- `scenarios/` — the benchmark corpus: one YAML file per scenario, `schema.json` (the JSON Schema every scenario is validated against), and `scenarios/holdout/` for scenarios held out of the day-to-day sweep
- `tools/` — exported tool-call schemas (`junos-tools.json`, `panos-tools.json`, `combined-tools.json`) that describe the tool surface a model is allowed to call
- `tests/` — pytest suite covering the CLI, core loading, scoring, the runner, and tool export
- `results/` — committed run manifests and model-selection decision records; see `results/README.md` for the manifest schema. These are a historical record, not something a normal PR needs to regenerate.
- `run_benchmark.py` / `run_holdout.sh` — operator scripts that sweep a fixed model list against a live Ollama endpoint; not part of CI

## Install, run, test, lint

```sh
# Editable install with dev extras (pytest, ruff)
pip install -e ".[dev]"
# or, if you use uv (this repo ships a uv.lock):
uv pip install -e ".[dev]"
```

Run the test suite:

```sh
pytest
```

Lint (required to pass in CI):

```sh
ruff check .
```

Validate the scenario corpus against `scenarios/schema.json`:

```sh
bench lint scenarios/
```

`bench lint` also rejects scenarios that reference a known lab device name (e.g. `demo-srx`) directly in `prompt`/`setup` without the `{{device}}` placeholder — see "Adding a new benchmark scenario" below.

### Running benchmarks (`bench run`)

`bench run` has two modes:

- **`--mode blind`** (default): single-pass, no tool execution, talks only to an OpenAI-compatible chat endpoint. This is what most scenario/scoring work should be validated against, and needs no lab access.
- **`--mode agentic`**: multi-turn, calls a real MCP server that fronts real Junos/PAN-OS lab devices, and requires an `RUSTJUNOSMCP_TOKEN` (and optionally `RUSTJUNOSMCP_SETUP_TOKEN` for fault setup/teardown). This is how `run_benchmark.py` and `run_holdout.sh` are actually run for model-selection sweeps.

A normal contribution — a bug fix, a new scenario, a scoring change — does not require lab access or agentic-mode runs. Use `--mode blind`, the unit tests, and `bench lint` to validate your change. Never point agentic mode at a production device.

## Adding a new benchmark scenario

Scenarios are YAML files in `scenarios/` (or `scenarios/holdout/` for an eval-only, never-swept-in-day-to-day-runs scenario). Each one is validated against `scenarios/schema.json`. Required fields, based on the existing corpus (see e.g. `scenarios/heal-junos-permissive-rule-01.yaml`):

```yaml
id: heal-junos-permissive-rule-01      # unique, kebab-case
vendor: junos                          # junos | panos | multi
setup: |                               # operator-applied config that creates the fault/baseline
  set security policies from-zone trust to-zone untrust policy demo-bad ...
prompt: >                              # task given to the agent
  Audit the trust->untrust policies on {{device}} and stage a fix ...
expected_calls:                        # ordered tool-call expectations
  - tool: get_junos_config
  - tool: create_junos_change_set
    args_contains: ["demo-bad"]
forbidden_calls:                       # any of these immediately fails the scenario
  - tool: apply_junos_change_set
scoring: outcome                       # outcome | outcome_lenient | all_expected_present_and_ordered_no_forbidden
outcome:                               # required detail when scoring: outcome
  staged_diff_contains: ["demo-bad"]
  must_not_commit: true
```

Guidelines:

- Always template the device under test as `{{device}}` in `prompt`/`setup` — never hardcode a real or lab hostname (`bench lint` enforces this for the known placeholder patterns, but don't rely on the linter alone).
- Use synthetic config (fake policy names, RFC 5737/TEST-NET addresses, etc.), never a real device's actual configuration.
- Prefer `scoring: outcome` (checks the staged diff / final report — an honest gate) over the strict ordered-call check unless you have a specific reason; see `scenarios/schema.json` for what each scoring mode checks.
- Run `bench lint scenarios/` before opening the PR.
- If your scenario exercises a new schema field or scoring path, add or extend a test in `tests/` (`test_scoring.py`, `test_outcome_scoring.py`, or `test_core.py` depending on what it touches).

## Benchmark claims

If your PR claims a performance or accuracy improvement ("model X is N% more accurate", "the new scoring mode is faster"), include the actual before/after numbers — a manifest under `results/` or a summary table from `analyze_results.py` — not an estimate. PRs that only add or improve *benchmarking capability itself* (a new CLI flag, a new scenario, a runner fix, corpus tooling) don't need to retroactively produce comparison numbers for the existing corpus; just say so in the PR description instead of leaving the checklist item unchecked and unexplained.

## Commit and PR conventions

- Match the existing commit style: `type(scope): summary` (`fix(teardown):`, `feat:`, `docs:`, `chore:`) — see `git log` for examples.
- Keep PRs focused on one change.
- Fill out the PR template, including the exact commands you ran to verify the change.
- All contributions land as a pull request against `main` for human review — there is no direct-push path to `main`.
- By opening a pull request, you're agreeing your contribution is licensed under this repository's [MIT license](LICENSE). This repo doesn't require a `Signed-off-by` / DCO line on commits — opening the PR is the licensing act.

## Review process

Every pull request goes through a security review and a code review, then an independent test run, before anything merges. Only a maintainer merges — contributors, including anyone with write access, should not merge their own PR. CI (lint, tests, dependency audit) must be green first.

## Reporting a vulnerability

Please don't open a public issue for a security vulnerability — see [SECURITY.md](SECURITY.md) for how to report one privately.

## Fixtures, secrets, and test data

Never commit real device configs, hostnames, serial numbers, or MCP bearer tokens (`RUSTJUNOSMCP_TOKEN`, `RUSTJUNOSMCP_SETUP_TOKEN`) — synthetic scenario data only. This repo's `.gitignore` blocks common token file patterns (`*.token`, `.env.*-token`) because a real one nearly got committed once; don't rely on `.gitignore` alone, review your diff before pushing. If you find real data already committed anywhere in this repo, don't add to it — report it privately instead (see [SECURITY.md](SECURITY.md)).
