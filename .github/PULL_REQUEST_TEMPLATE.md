## Summary

<!-- What does this PR do, and why? -->

## Changes

<!-- Bullet list of what changed -->

## Verification

<!-- Exact commands you ran and their result. "Should work" is not verification. -->

```sh

```

## Benchmark methodology impact

<!-- Does this change affect scoring logic, the scenario corpus, tool schemas, or anything else that would make results/ manifests from before and after this change not directly comparable? If yes, explain what changed and whether existing results/ records need a note. If no, say "no impact". -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `pytest` passes
- [ ] `bench lint scenarios/` passes (if you added/changed a scenario file)
- [ ] Tests added or updated for this change, and they fail against the old code
- [ ] No secrets, credentials, real hostnames, serials, or real device configs in code, tests, fixtures, results, or this description
- [ ] No new telemetry, analytics, or outbound network call added
- [ ] Any benchmark/performance claim in this PR ships with actual before/after numbers, or this PR only adds/changes benchmarking capability and doesn't need retroactive numbers (say which, above)
- [ ] If this touches agentic-mode device interaction: it stages, never auto-applies, and teardown still runs by default

## Anything you're unsure about

<!-- Flag it here rather than hoping review catches it -->
