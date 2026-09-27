# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Instead, use GitHub's private vulnerability reporting for this repository:

https://github.com/fastrevmd-lab/mechubbench/security/advisories/new

Include what you'd include in a bug report — affected version/commit, reproduction steps, and impact — but keep it in the private report, not a public issue, PR, or discussion.

## Scope

mechubbench is a benchmark harness that, in its "agentic" runner mode, drives a real MCP server fronting lab Junos/PAN-OS devices and records the resulting tool-call transcripts and staged diffs. Vulnerability classes we especially want to hear about:

- Anything that could cause the harness to apply/commit a device change rather than merely stage one, or to skip the teardown of a staged change-set (device state left live outside of a scored, opt-in run)
- MCP bearer token (`RUSTJUNOSMCP_TOKEN`, `RUSTJUNOSMCP_SETUP_TOKEN`) handling issues — leaking a token into a committed manifest, log line, or error message
- Scenario/schema loading issues that could let a malicious scenario YAML or tool-schema JSON achieve more than declarative data (e.g. via unsafe YAML loading or path traversal when reading `scenarios/` or `tools/`)
- Anything that could cause a committed `results/*.json` manifest or scenario fixture to carry real device configuration, hostnames, or credentials

## Not in scope

The benchmark corpus intentionally includes scenarios that instruct a model to attempt unsafe or overly permissive firewall changes (that's the point — scoring whether the model does the safe thing). That corpus content is not itself a vulnerability.

## Response

This is a community-maintained project. There's no guaranteed SLA, but reports are read and triaged by a human maintainer, not by any automated or model-based process.
