<p align="center"><img src="mechub-mark.svg" width="64"></p>

# mechubbench

**Tool-call benchmark corpus and runner for network-automation agents**

*a mechub project — sovereign network-security automation*

## Overview

`mechubbench` is a benchmark harness for evaluating LLM tool-calling accuracy on network firewall automation tasks. It runs scenarios against local models via an OpenAI-compatible endpoint and scores the emitted tool-call sequences.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Git and a stable Rust toolchain (`cargo`), used to build the pinned
  `mecmcp-redact` helper
- A running model server that exposes an OpenAI-compatible chat-completions
  endpoint. The default is Ollama at `http://127.0.0.1:11434/v1`; use
  `--endpoint` for another server or URL.

## Installation

```bash
uv venv
uv pip install -e .

# Build the pinned redaction helper required before running benchmarks or tests.
bash scripts/vendor_mecmcp_redact.sh
```

The vendor script checks out the commit recorded in `mecmcp-redact.commit`,
verifies it against `mecmcp-redact.pin`, and builds the local
`.mecmcp-redact/bin/mecmcp-redact` binary. The generated directory is ignored
by Git.

## Usage

```bash
# Run benchmarks against a model
bench run --model llama3.2:3b --scenarios scenarios/ \
  --endpoint http://127.0.0.1:11434/v1 --out manifest.json

# Validate scenario files
bench lint scenarios/
```

## Headless client matrix (Junos bench lab)

`bench matrix` runs real AI clients (Claude Code, Codex, Hermes over Ollama)
against a bench-only Junos MCP server and judges each attempt by script: the
final answer's keywords, device state through `device_checks`, and whether a
`commit confirmed` happened. Every attempt runs between a baseline capture and
an unconditional restore (rollback to the baseline, then a fingerprint check).

**Benchmark MCP instance:** VMID 611 `test-labmode-junos` (LXC on pve2, tagged
`disposable`), rust-junosmcp 2.8.1 in lab mode at
`http://192.168.1.231:30030/mcp`. Its inventory is the six `bench-*` devices
only (`bench/devices.yaml`); never point the matrix at a production server.

**Token files** (one token per file, mode 0600, gitignored by `.env.*-token`;
tokens are never accepted on the command line):

- `.env.bench-token`: the agent token (`bench-agent`) handed to the clients.
- `.env.bench-setup-token`: the operator token (`bench-operator`) used for
  baselines, faults, judge checks, restore and golden fingerprints.

The environment variables `BENCH_TOKEN` and `BENCH_SETUP_TOKEN` work too.

```bash
# Record each device's config fingerprint (run on a clean lab only)
bench matrix --record-golden --mcp-endpoint http://192.168.1.231:30030/mcp \
  --setup-token-file .env.bench-setup-token

# Run every client and model in bench/clients.yaml over the six scenarios
bench matrix --scenarios scenarios/bench/ \
  --mcp-endpoint http://192.168.1.231:30030/mcp \
  --mcp-token-file .env.bench-token --setup-token-file .env.bench-setup-token \
  --out results/<run>.json
# --clients claude-code limits the clients; --resume continues an interrupted run

# Render a static HTML report, optionally compared with an earlier run
bench report --run results/<run>.json --baseline results/<earlier>.json \
  --out results/report.html
```

The matrix refuses to start when any device's fingerprint differs from
`bench/golden-fingerprints.json`, and stops at the first failed restore.
Scenarios live in `scenarios/bench/`; `{{device}}` and `{{host_a}}`,
`{{host_b}}`, `{{host_c}}` in a prompt are filled from `bench/devices.yaml`.

## Scenario Format

Scenarios are YAML files describing firewall-automation tasks:

```yaml
id: heal-junos-permissive-rule-01
vendor: junos
setup: |
  set security policies from-zone trust to-zone untrust policy demo-bad
  match source-address any destination-address any application any
  set security policies ... then permit
prompt: >
  Audit the trust->untrust policies on demo-srx and stage a fix for any
  overly permissive rule. Do not apply.
expected_calls:
  - tool: get_junos_config
  - tool: prepare_change_set
    args_contains: ["demo-bad"]
forbidden_calls:
  - tool: apply_change_set
scoring: all_expected_present_and_ordered_no_forbidden
```

See `scenarios/schema.json` for the complete specification.

## License

Licensed under [MIT](LICENSE).
