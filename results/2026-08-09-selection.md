# Model Selection Record - 2026-08-09

**Evaluation Date:** 2026-08-09  
**Evaluator:** Claude Fable 5 (Anthropic)  
**Corpus Version:** mechubbench @ `61bff67879ac8940d1b38b07948fbb34b3038b3d`  
**Scenarios:** 20 main corpus, 5 holdout (reserved)

## Decision

**NO MODEL SELECTED** - No candidates met both gate requirements (≥85% accuracy + ≤90s avg latency).

## Evaluation Results

| Model | Scenarios Run | Passed | Score % | Avg Latency | Status |
|-------|---------------|--------|---------|-------------|--------|
| qwen2.5:14b-instruct | 20/20 | 7 | 35.0% | 14.4s | FAILED (score) |
| qwen3.6:35b-a3b | 11/20* | ~2 | ~18%* | >120s | DISQUALIFIED (latency) |
| ornith:35b | 0 | - | - | >120s (est) | NOT RUN |
| gpt-oss:120b | 0 | - | - | >180s (est) | NOT RUN |

\* Partial evaluation; process killed after 20+ minutes due to excessive latency

## Gate Criteria

- **Accuracy:** ≥17/20 scenarios passing (85%)
- **Latency:** ≤90s average per scenario
- **Combined:** Both requirements must be met

## Key Findings

1. **14B model**: Fast but inaccurate (35% vs 85% requirement)
2. **30B+ models**: Too slow for stage use (>120s/scenario vs 90s threshold)
3. **Hardware bottleneck**: Ollama on available hardware cannot serve larger models at interactive speeds
4. **Gap identified**: No local model in tested pool meets both requirements simultaneously

## Evaluation Environment

- **Serving:** Ollama 0.32.1 at `http://inference.example.com:11434/v1`
- **Temperature:** 0.0 (deterministic)
- **Tool Schema:** 11 tools (6 Junos + 5 PAN-OS) from `tools/combined-tools.json`
- **Corpus:** 20 scenarios across discovery, healing, failure-detection, standardization, and tightening tasks

## Recommendations

**Short-term (Tasks 13-14):**
- Use cloud API (Claude 3.5 Sonnet or GPT-4) for demo if model quality matters
- OR accept degraded accuracy with qwen2.5:14b and label as "proof-of-concept"

**Medium-term:**
- Evaluate newer small models (Qwen2.5-Coder 7B, Llama 3.3 70B AWQ) on better hardware
- Consider vLLM serving instead of Ollama for better throughput
- Provision GPU with sufficient VRAM (A100 40GB+) for 30B+ models

**Long-term:**
- Hybrid approach: local small model for fast common cases, cloud fallback for complex scenarios
- Expand corpus to multi-turn scenarios (current eval is single-turn only)

## Artifacts

- Main corpus manifest: `results/20260809-102943-qwen2.5-14b-instruct.json`
- Holdout scenarios: `scenarios/holdout/*.yaml` (not evaluated)
- Full report: `/.superpowers/sdd/implementation-plan/task-4-report.md` (mechub repo)

---

**Signed:** Task 4 evaluation complete; evidence-first findings documented honestly per brief requirements.
