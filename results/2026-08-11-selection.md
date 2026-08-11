# Stage-model selection — 2026-08-11

**Decision: pin `ornith:35b` as the demo stage model.**

Best of the three candidates on the first fully valid sweep (6/12), and the
only model that reliably completes the create-change-set staging workflow.
All three local models are marginal in absolute terms — demo choreography
must draw from the scenario families where the pinned model is proven
(see "Demo guidance").

## Environment

- Server: rust-junosmcp **0.18.0** on LXC 609 (`198.51.100.194:30031`),
  first sweep on the cancel-capable release
- Inference: Ollama on strix (`198.51.100.108:11434`, endpoint pinned by IP
  after resolver flakes killed a full run), temperature 0, agentic mode
- Device: vsrx-ci, fault-staged per scenario via the setup token
  (load_and_commit → run → rollback 1 → verified-clean candidate)
- Agent tool surface: the same 11 read/stage/discard tools as sweeps 7/8
  (cancel_junos_change_set is harness-teardown only, for comparability)
- Scoring: outcome mode (staged content + report keywords + must-not-commit)

## Why sweeps 7/8 are struck

Sweep 9 is the **first valid sweep**. Three harness defects — every one
exposed by the isError check that shipped with the cancel work — were found
and fixed during its runs:

1. **Fault setups never ran in sweeps 7/8.** The runner sent `config`;
   the server requires `config_text` and denies unknown fields. Pre-isError,
   the tool error was swallowed and "Fault setup committed" logged anyway.
   Sweeps 7/8 measured models against an unfaulted device. (`c185866`)
2. **Setup blocks were never device-valid.** `#` comments (rejected by
   Junos set-format load) in 11/12 scenarios — now stripped centrally by
   `prepare_setup_config()`; a bare `commit` line in one scenario; DNS-less
   device couldn't resolve `pool.ntp.org` NTP hostnames in another (now
   TEST-NET IPs). All 12 setups now validated live: stage → commit →
   rollback → clean candidate. (`c185866`)
3. **Staged content was never captured.** `get_junos_change_set_status`
   returns metadata only (digest, state, action_count) — never the payload —
   so every `staged_diff_contains` assertion failed regardless of what the
   model staged. Capture now takes the actions payload from the create call
   the server accepted (fingerprint-gated, digest-bound). The old capture
   test mocked a `payload` field the real server never returns. (`8b02e54`)

One aborted run additionally established the endpoint-by-IP rule: local DNS
(198.51.100.1) intermittently failed to resolve `inference.example.com` mid-run,
killing all 36 scenario runs.

## Sweep 9 (dev set: 12 Junos scenarios, outcome scoring)

| Model | Pass | Notes |
|---|---|---|
| **ornith:35b** | **6/12** | Only model passing heal-add-ntp AND heal-permissive; staged real change-sets |
| qwen3.6:35b-a3b | 5/12 | Stages sometimes; wrong content on 2 (dns scenarios) |
| qwen2.5:14b-instruct | 3/12 | Never staged a change-set anywhere; 3 scenarios hit the 12-turn limit |

Failure taxonomy (verified against transcripts, not just reasons):

- **Doesn't stage**: the dominant failure. Models reach for
  `commit_check_config` (validation) instead of `create_junos_change_set`
  (staging) and finish without staging anything. 14b does this universally;
  a3b and ornith on the fleet-sync/rollback/standardize-ntp scenarios.
- **Stages wrong content**: a3b and ornith each staged plausible-but-wrong
  configs on one dns scenario (missing the required 1.1.1.2 server).
- **Op-command fumbling**: repeated `execute_junos_command` syntax errors
  (config commands sent to the op-command tool) burning turns.

All three models pass the read-only discover scenario and both tighten
scenarios (policy analysis + staged cleanup) — the strongest family.

## Holdout (3 unseen Junos scenarios, ornith:35b only)

**1/3 PASS.**

- PASS `tighten-junos-orphaned-address-object`
- FAIL `fail-junos-duplicate-rule-shadowing` — burned all 12 turns
  re-reading config and fumbling op commands; never produced the report
- FAIL `heal-junos-nat-hairpin-misconfig` — 12-turn limit, nothing staged
  (hard scenario: multi-step NAT reasoning)

Holdout note: two holdout scenarios predated the outcome-scoring decision —
one *expected* `apply_junos_change_set`, which the bench safety rails never
invoke. Both were aligned to the bench contract before the scored run
(`817925c`); difficulty was not changed. PAN-OS holdouts
(2) remain unrun pending a rustpanosmcp bench endpoint.

## Demo guidance

- ornith:35b is reliable on: **discover** (report-only), **tighten**
  (analyze + stage cleanup), **fail-any-any** (audit + stage fix),
  **heal-add-ntp / heal-permissive-rule** (fault → stage fix). Build the
  demo beats from these families; rehearse the exact scenario instances.
- Avoid unrehearsed multi-step reasoning scenarios (NAT hairpin class) on
  local models.
- Keep temperature 0 and the 11-tool commitless surface; the harness
  teardown uses cancel_junos_change_set (v0.18.0+).

## Follow-ups

- Product gap: approvers see only digest + action_count —
  `get_junos_change_set_status` never returns the staged actions, so the
  approval webapp can't show what is being approved. Needs a server-side
  review view (mecmcp + rustjunosmcp) before the demo's approval beat is
  honest. Decision pending.
- Consider a scenario-prompt nudge ("stage with create_junos_change_set")
  as a *separate* bench variant if we want to measure tool-selection help;
  do not silently change the dev set.
