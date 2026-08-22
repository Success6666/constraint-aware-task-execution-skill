# A/B Evaluation Report

- Model: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Cases: `30` (`15` English, `15` Chinese)
- Categories: `12` hard constraints, `6` soft preferences, `6` safety/explicit enforcement, `6` output/architecture constraints

Completed pairs: `30/30`

## Aggregate Results

| Metric | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `evaluation_pass` | 1.0000 | 1.0000 | +0.0000 |
| `required_pass` | 1.0000 | 1.0000 | +0.0000 |
| `objective_coverage` | 1.0000 | 1.0000 | +0.0000 |
| `constraint_adherence` | 1.0000 | 1.0000 | +0.0000 |
| `constraint_violation_hits` | 0.0000 | 0.0000 | +0.0000 |
| `required_enforcement_coverage` | 1.0000 | 1.0000 | +0.0000 |
| `under_enforcement_hits` | 0.0000 | 0.0000 | +0.0000 |
| `failure_gate_hits` | 0.0333 | 0.0000 | -0.0333 |
| `constraint_component_hits` | 0.1000 | 0.0000 | -0.1000 |
| `constraint_echo` | 0.4333 | 0.1667 | -0.2666 |
| `soft_preference_hardening` | 0.0000 | 0.0000 | +0.0000 |

Higher is better for pass, coverage, and adherence metrics; lower is better for the remaining metrics.

## Qualified Over-Optimization

Only the `30` pairs where both responses passed objective and constraint gates are compared.

| Metric | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `overoptimization_score` | 0.8333 | 0.1667 | -0.6666 |

## Per-Case Results

| Case | Baseline Pass | Skill Pass | Baseline Score | Skill Score | Delta |
| --- | :---: | :---: | ---: | ---: | ---: |
| `auth-two-files-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `billing-modular-monolith-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `client-avoid-singleton-zh` | yes | yes | 5.00 | 1.00 | -4.00 |
| `config-no-yaml-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `crawler-prefer-stdlib` | yes | yes | 0.00 | 0.00 | +0.00 |
| `csv-no-pandas` | yes | yes | 0.00 | 0.00 | +0.00 |
| `etl-no-spark` | yes | yes | 0.00 | 0.00 | +0.00 |
| `events-no-kafka-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `fastapi-no-celery-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `fastapi-no-redis` | yes | yes | 0.00 | 0.00 | +0.00 |
| `image-upload-safety-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `inventory-no-orm` | yes | yes | 0.00 | 0.00 | +0.00 |
| `jobs-no-celery` | yes | yes | 0.00 | 0.00 | +0.00 |
| `json-output-only-en` | yes | yes | 0.00 | 0.00 | +0.00 |
| `json-output-only-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `license-ci-enforcement` | yes | yes | 0.00 | 0.00 | +0.00 |
| `local-dev-no-docker-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `log-analyzer-fewer-dependencies-zh` | yes | yes | 3.00 | 2.00 | -1.00 |
| `notifications-avoid-global-state` | yes | yes | 0.00 | 0.00 | +0.00 |
| `orders-modular-monolith-en` | yes | yes | 0.00 | 0.00 | +0.00 |
| `parser-three-files-en` | yes | yes | 0.00 | 0.00 | +0.00 |
| `qa-no-langchain` | yes | yes | 0.00 | 0.00 | +0.00 |
| `rate-limit-avoid-global-zh` | yes | yes | 13.00 | 2.00 | -11.00 |
| `search-no-elasticsearch-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `secret-scan-ci-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `tests-no-mocks-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `upload-block-executables` | yes | yes | 0.00 | 0.00 | +0.00 |
| `url-fetcher-ssrf` | yes | yes | 0.00 | 0.00 | +0.00 |
| `webhook-fewer-dependencies` | yes | yes | 0.00 | 0.00 | +0.00 |
| `webhook-replay-safety-zh` | yes | yes | 0.00 | 0.00 | +0.00 |

## Interpretation

The aggregate over-optimization score decreased by `80.0%` without reducing aggregate objective coverage.

Aggregate constraint adherence changed by `+0.0000`.

Per case: `7` improved, `0` worsened, and `23` tied.

This is a small, single-model regression experiment with a strong baseline. The deterministic scorer is not a statistical significance test or a substitute for blind human review. Inspect the committed raw responses when a metric changes unexpectedly.

## Capability Retention

- Paired rows: `30/30`
- Quality retention ratio: `1.0000`
- Non-constraint requirement retention: `1.0000`
- Capability regression rate: `0.0000`
- Efficiency regression rate: `0.0000`
- Valid information retention: `n/a`
- Semantic review coverage: `0.0000`
- Cost ratio: `n/a`
- Latency ratio: `n/a`
- General semantic capability: `partial`
- Capability acceptance: `pass`
- Semantic review required: `False`

Only successful baseline/skill pairs enter retention denominators. Missing pairs are reported as coverage gaps, not zero scores. General semantic preservation remains partial unless an explicit semantic evaluator supplies observations.

| Component | Observed Pairs | Missing Candidate Evidence | Retention | Regression Rate |
| --- | ---: | ---: | ---: | ---: |
| `objective_coverage` | 30 | 0 | 1.0000 | 0.0000 |
| `non_constraint_requirement_coverage` | 0 | 0 | n/a | 0.0000 |
| `declared_quality_score` | 0 | 0 | n/a | 0.0000 |
| `constraint_compliance` | 30 | 0 | 1.0000 | 0.0000 |
| `format_compliance` | 30 | 0 | 1.0000 | 0.0000 |
| `path_compliance` | 30 | 0 | 1.0000 | 0.0000 |
| `artifact_contract` | 0 | 0 | n/a | 0.0000 |

Behavioral regression observations:

- `unnecessary_refusal`: `0/30` (rate `0.0000`)
- `unnecessary_clarification`: `0/30` (rate `0.0000`)
- `over_conservative`: `0/30` (rate `0.0000`)
