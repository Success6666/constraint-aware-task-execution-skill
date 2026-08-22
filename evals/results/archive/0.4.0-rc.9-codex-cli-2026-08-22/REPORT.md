# A/B Evaluation Report

- Model: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Cases: `30` (`15` English, `15` Chinese)
- Categories: `12` hard constraints, `6` soft preferences, `6` safety/explicit enforcement, `6` output/architecture constraints

Completed pairs: `30/30`

## Aggregate Results

| Metric | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `evaluation_pass` | 0.9667 | 0.9667 | +0.0000 |
| `required_pass` | 0.9667 | 1.0000 | +0.0333 |
| `objective_coverage` | 1.0000 | 0.9933 | -0.0067 |
| `constraint_adherence` | 0.9667 | 1.0000 | +0.0333 |
| `constraint_violation_hits` | 0.0000 | 0.0000 | +0.0000 |
| `required_enforcement_coverage` | 1.0000 | 1.0000 | +0.0000 |
| `under_enforcement_hits` | 0.0000 | 0.0000 | +0.0000 |
| `failure_gate_hits` | 0.0000 | 0.0000 | +0.0000 |
| `constraint_component_hits` | 0.0333 | 0.0000 | -0.0333 |
| `constraint_echo` | 0.6000 | 0.2333 | -0.3667 |
| `soft_preference_hardening` | 0.0000 | 0.0000 | +0.0000 |

Higher is better for pass, coverage, and adherence metrics; lower is better for the remaining metrics.

## Qualified Over-Optimization

Only the `28` pairs where both responses passed objective and constraint gates are compared.

| Metric | Baseline | Skill | Delta |
| --- | ---: | ---: | ---: |
| `overoptimization_score` | 0.7500 | 0.2500 | -0.5000 |

## Per-Case Results

| Case | Baseline Pass | Skill Pass | Baseline Score | Skill Score | Delta |
| --- | :---: | :---: | ---: | ---: | ---: |
| `auth-two-files-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `billing-modular-monolith-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `client-avoid-singleton-zh` | yes | yes | 2.00 | 0.00 | -2.00 |
| `config-no-yaml-zh` | yes | no | 0.00 | 2.00 | +2.00 |
| `crawler-prefer-stdlib` | yes | yes | 0.00 | 0.00 | +0.00 |
| `csv-no-pandas` | yes | yes | 0.00 | 0.00 | +0.00 |
| `etl-no-spark` | yes | yes | 0.00 | 0.00 | +0.00 |
| `events-no-kafka-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `fastapi-no-celery-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `fastapi-no-redis` | yes | yes | 0.00 | 0.00 | +0.00 |
| `image-upload-safety-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `inventory-no-orm` | yes | yes | 4.00 | 1.00 | -3.00 |
| `jobs-no-celery` | yes | yes | 0.00 | 0.00 | +0.00 |
| `json-output-only-en` | no | yes | 0.00 | 0.00 | +0.00 |
| `json-output-only-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `license-ci-enforcement` | yes | yes | 0.00 | 0.00 | +0.00 |
| `local-dev-no-docker-zh` | yes | yes | 1.00 | 0.00 | -1.00 |
| `log-analyzer-fewer-dependencies-zh` | yes | yes | 0.00 | 1.00 | +1.00 |
| `notifications-avoid-global-state` | yes | yes | 2.00 | 0.00 | -2.00 |
| `orders-modular-monolith-en` | yes | yes | 0.00 | 0.00 | +0.00 |
| `parser-three-files-en` | yes | yes | 0.00 | 0.00 | +0.00 |
| `qa-no-langchain` | yes | yes | 0.00 | 0.00 | +0.00 |
| `rate-limit-avoid-global-zh` | yes | yes | 5.00 | 4.00 | -1.00 |
| `search-no-elasticsearch-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `secret-scan-ci-zh` | yes | yes | 0.00 | 0.00 | +0.00 |
| `tests-no-mocks-zh` | yes | yes | 4.00 | 0.00 | -4.00 |
| `upload-block-executables` | yes | yes | 0.00 | 0.00 | +0.00 |
| `url-fetcher-ssrf` | yes | yes | 0.00 | 0.00 | +0.00 |
| `webhook-fewer-dependencies` | yes | yes | 2.00 | 1.00 | -1.00 |
| `webhook-replay-safety-zh` | yes | yes | 0.00 | 0.00 | +0.00 |

## Interpretation

No overall improvement claim is made because the qualified baseline score was zero, task/constraint pass rates decreased, or the capability-retention gate did not pass.

Aggregate constraint adherence changed by `+0.0333`.

Per case: `9` improved, `2` worsened, and `19` tied.

This is a small, single-model regression experiment with a strong baseline. The deterministic scorer is not a statistical significance test or a substitute for blind human review. Inspect the committed raw responses when a metric changes unexpectedly.

## Capability Retention

- Paired rows: `30/30`
- Quality retention ratio: `0.9983`
- Non-constraint requirement retention: `0.9978`
- Capability regression rate: `0.0333`
- Efficiency regression rate: `0.0000`
- Valid information retention: `n/a`
- Semantic review coverage: `0.0000`
- Cost ratio: `n/a`
- Latency ratio: `n/a`
- General semantic capability: `partial`
- Capability acceptance: `fail`
- Semantic review required: `False`

Only successful baseline/skill pairs enter retention denominators. Missing pairs are reported as coverage gaps, not zero scores. General semantic preservation remains partial unless an explicit semantic evaluator supplies observations.

| Component | Observed Pairs | Missing Candidate Evidence | Retention | Regression Rate |
| --- | ---: | ---: | ---: | ---: |
| `objective_coverage` | 30 | 0 | 0.9933 | 0.0333 |
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

Capability gate failures:

- `skill`: `quality_retention, non_constraint_requirement_retention, paired_regression, missing_efficiency_evidence`
