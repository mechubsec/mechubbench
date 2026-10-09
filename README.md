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
