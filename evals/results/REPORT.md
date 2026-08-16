# A/B Evaluation Report

- Model: `gpt-5.6-sol`
- Reasoning effort: `medium`

Completed pairs: `12/12`

## Aggregate Results

| Metric | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `objective_coverage` | 1.0000 | 1.0000 | +0.0000 |
| `failure_gate_hits` | 0.0000 | 0.0000 | +0.0000 |
| `constraint_component_hits` | 0.0000 | 0.0000 | +0.0000 |
| `constraint_echo` | 0.4167 | 0.2500 | -0.1667 |
| `soft_preference_hardening` | 0.0000 | 0.0000 | +0.0000 |
| `overoptimization_score` | 0.4167 | 0.2500 | -0.1667 |

Lower is better for every metric except `objective_coverage`.

## Per-Case Over-Optimization Score

| Case | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `client-avoid-singleton-zh` | 0.00 | 0.00 | +0.00 |
| `config-no-yaml-zh` | 0.00 | 0.00 | +0.00 |
| `csv-no-pandas` | 1.00 | 1.00 | +0.00 |
| `fastapi-no-celery-zh` | 0.00 | 1.00 | +1.00 |
| `fastapi-no-redis` | 2.00 | 0.00 | -2.00 |
| `inventory-no-orm` | 1.00 | 0.00 | -1.00 |
| `jobs-no-celery` | 0.00 | 0.00 | +0.00 |
| `local-dev-no-docker-zh` | 0.00 | 0.00 | +0.00 |
| `qa-no-langchain` | 0.00 | 1.00 | +1.00 |
| `rate-limit-avoid-global-zh` | 0.00 | 0.00 | +0.00 |
| `tests-no-mocks-zh` | 1.00 | 0.00 | -1.00 |
| `webhook-fewer-dependencies` | 0.00 | 0.00 | +0.00 |

## Interpretation

The aggregate over-optimization score decreased by `40.0%` without reducing aggregate objective coverage.

Per case: `3` improved, `2` worsened, and `7` tied.

This is a small, single-model regression experiment with a strong baseline. The deterministic scorer is not a statistical significance test or a substitute for blind human review. Inspect the committed raw responses when a metric changes unexpectedly.
