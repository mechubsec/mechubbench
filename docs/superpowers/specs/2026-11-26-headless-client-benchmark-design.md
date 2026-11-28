# Headless-client Junos benchmark — design

Date: 2026-11-26. Status: draft for review.

## Intent

Measure how well real AI coding clients do real Junos work through rust-junosmcp, so the
upcoming per-release catalog and skills work (rustjunosmcp branch `skills-n-docs`) can be
shown to help — or not. The baseline run uses the current released rust-junosmcp; later
runs use builds with the catalog. The output the user looks at is **a chart of token use
and speed, per model and per test, with pass/fail on it**.

## Goals

- Run the same six Junos tasks through each client, on every applicable bench device, and
  judge every result with a script.
- Capture tokens (input, output, cache read, cache write) and wall time per attempt.
- Show pass/fail and how often each model fails, next to its cost and speed.
- Compare two runs (before/after) on the same page.
- Keep these as general mechubbench features, not one-off scripts.

## Non-goals

- No LLM judge for now. It stays an option if scripted judging proves unworkable for a task.
- No GitHub Copilot driver yet (no licence here). The driver interface must not preclude it.
- The existing direct-API runner (`runner.py`, OpenAI-compatible `/chat/completions`) stays
  in the code but is not used by this benchmark.

## Anti-goals

- A cheap, fast, wrong answer must never look like a win on the chart.
- The benchmark must never touch a non-bench device or prod-labmode-junos.

## Decisions (made by the user)

### Clients and models

| Client | Models | Notes |
|---|---|---|
| Claude Code (headless) | the vendor's latest flagship | |
| Codex CLI (headless) | the vendor's latest flagship | |
| Hermes agent | every Ollama model on strix | Ollama models run **only** through Hermes |
| Copilot | — | deferred |

Ollama models on strix as of 2026-11-26: qwen3.8:27b, qwen3.5:122b, glm-4.7-flash,
qwen3-coder-next, gpt-oss:120b, ornith:35b. "All models downloaded" means the list at run
time. A "model" on the chart is client + model (the same model under two clients would be
two entries).

2026-11-28: glm-4.7-flash and gpt-oss:120b are dropped from scope. They are listed under
`hermes.exclude` in `bench/clients.yaml`, so newly pulled models are still picked up.

If an Ollama model is a full failure on more than one test, the report flags it so the user
can decide whether to drop it. Nothing is dropped automatically.

### Client isolation

Every client (Claude Code, Codex, Hermes) runs with **only the benchmark MCP server** and
all built-in tools off: no shell, no file edit, no web. Each gets an isolated config/HOME,
so no other MCP server registered on the machine is reachable. Results then measure work
done through rust-junosmcp, and the `bench-*` token scope is the only route to a device.

### Token comparison

The chart puts **total tokens (input + output)** for every client on one shared axis. Cache
read and cache write are separate columns, blank where a client does not report them. A
note on the page says the clients count tokens slightly differently.

### Judging and retries

- Scripts judge everything. Each test defines its pass check (answer content and/or device
  state afterwards).
- Each model × test × device combination gets up to **three attempts**: run once; on fail,
  retry; stop at the first pass.
- Three fails = **full failure**: its token and time numbers are not charted.
- A pass after failures charts the passing attempt's tokens and time, plus the failure
  count (e.g. "pass after 2 fails").

### Devices and safety

- A **separate benchmark rust-junosmcp instance**, holding only the `bench-*` devices, in
  the 600-range test guests (VMID 611 `test-labmode-junos`, tagged `disposable;labmode;test`,
  currently stopped, is the fit). Swapping the server version for before/after happens there.
- The benchmark token is scoped to `bench-*` devices. Safety comes from that scope, because
  the headless clients run their own agent loops.
- Real commits are allowed. Every test ends with the device back at its baseline. The next
  test starts only after the existing fingerprint check passes (empty diff, fingerprint
  matches the pre-test baseline); if it fails, the run stops.
- Read-only tests skip rollback but still run the fingerprint check.

### Platforms

Each test declares the platforms it applies to and runs on every matching bench device. The
chart can break a model's result down by platform: EX, classic SRX, Evolved SRX, vSRX, cSRX.

Bench devices (see `~/homelab/runbooks/2026-11-24-junos-bench-lab.md`): bench-ex2300
(23.4R2-S2.1), bench-srx1600 (25.4R1.12), bench-srx440 (25.4X300-D40.2-EVO), bench-srx345
(25.4R1-S2.3, no management instance), bench-vsrx-inet (26.2R1.7), bench-csrx (26.2R1.7);
test hosts A/B/C at 10.66.151.10, 10.66.162.10, 10.66.171.10.

### Output

A **static HTML page in the repo**, regenerated per run, showing tokens and speed per model
per test with pass/fail and failure frequency, and a before/after comparison of two runs.
Where a client does not report a token field (e.g. cache), the cell is blank, never guessed.

### The six tests

| # | Test | Prompt (gist) | Devices | Writes | Pass when |
|---|---|---|---|---|---|
| 1 | Read and explain | Explain this device's security config | every SRX | no | answer names the zones, `permit-lab`, `deny-all`, the IPsec peer and source NAT, checked against the known config |
| 2 | EX MAC/port lookup | Which port and VLAN is Host A's MAC on? | bench-ex2300 | no | correct port and VLAN |
| 3 | SRX tcp/443 troubleshoot | Host A can't reach Host B on 443 — why? (script plants a fault first, e.g. HTTPS removed from `permit-lab` on one SRX) | SRX in the path | no (model diagnoses only; script removes the fault) | answer names the right device and policy |
| 4 | Safe change | Allow tcp/8080 from Host A to Host B | each SRX in the path | yes | committed with commit confirmed and the diff matches the expected change; then rolled back |
| 5 | Version trap | Set DNS servers 1.1.1.2 and 1.0.0.2 | boxes with `management-instance`: SRX1600, SRX440, vSRX | yes | name servers carry `routing-instance mgmt_junos` and a lookup on the device resolves; then rolled back |
| 6 | End-to-end flow trace | Trace Host A → Host C on tcp/22 | switch + firewalls | no | hops in order incl. the hub hairpin, plus zone and policy on each SRX |

"Version trap" means: a task whose obvious answer breaks on that platform or release; the
script checks the real outcome, not the syntax.

## What already exists in mechubbench (reuse)

- Scenario YAML + `scenarios/schema.json`; scripted scoring in `scoring.py` (expected and
  forbidden calls, staged-diff checks, `report_contains`).
- Fault setup via `load_and_commit_config`, revert via `rollback_config`, fingerprint/empty-
  diff guard that aborts the sweep (`runner.py:1275-1360`).
- JSON run manifest per run (`core.py`); `assemble_manifest` currently drops fields the
  runner produces (`scoring_mode`, `final_message`, `outcome_evidence`, `left_staged`).
- Missing today: headless-client drivers, token capture, per-attempt timing, retries,
  device-per-platform selection, charts, before/after.

## Constraints

- Local `~/Projects/mechubbench` `main` has diverged from `origin/main` (46 ahead, 63 behind,
  rewritten SHAs; origin has ~18 newer commits incl. device pinning). Build on `origin/main`:
  if every local-only commit is a rewritten copy of an origin commit, back up local `main`
  to a branch and reset it to origin; if any local commit has no origin twin, stop and show
  the user.
- No secrets in the repo, results or HTML: MCP tokens stay in the existing gitignored 0600
  files; transcripts must pass through redaction before they are written.
- VMID 611 is `disposable`; still resolve name and tag before any action on it. Never
  touch a `protected` guest. Record its address in strix `~/homelab/ipam/homelab.md`.

## Calls made by Claude (user to check)

- **Runs are serial.** Shared devices, real commits and one Ollama host force it. The
  harness can resume a partial run from its manifest.
- **Wall time covers the client's own run only.** It starts at client launch and ends at
  client exit, so fault setup, rollback and the fingerprint check are excluded.
- **Infra errors are their own outcome.** A client crash, an Ollama OOM, an MCP-server error
  or a device-unreachable error is recorded as `infra_error`. It does not use up one of the
  three attempts and is re-run. A timeout counts as a failed attempt.
- **Full failures show as a marked cell** and are left out of every average.
- **Unit of result:** model × test × device. A test cell shows passes over devices
  (e.g. 4/5) and expands to show each device.
- **Expected answers are fixtures** derived from
  `~/homelab/topology/bench-lab/final-configs/`. Each run starts by checking every bench
  device against its golden config fingerprint and refuses to start if one has drifted.
  This keeps before/after comparable.
- **Restore is unconditional.** After each write test, the script rolls back to the
  pre-test baseline, whatever the model did, including a plain commit or a commit confirmed
  that is still pending. It then runs the fingerprint check.
- **Server version comes from the server.** The harness records the version the benchmark
  instance reports. Claude builds and deploys each version to the 611 instance.
- **Prompts give names and addresses.** Hosts and devices are named with their IPs, for
  example "Host A 10.66.151.10". The tests measure Junos work, not discovery.

## Left to the builder

- Driver interface shape and how each client is invoked headless (flags, MCP config file,
  how its usage/token numbers are read back).
- How the HTML is produced (template, chart library inlined, no network needed to view).
- Run manifest schema extensions (attempts, tokens, device, platform, client, model id).
- How "latest flagship" is resolved; the exact model id used must be recorded per result.
- Concrete fault for test 3 and expected diff for test 4 per device.
- Timeouts per attempt.
