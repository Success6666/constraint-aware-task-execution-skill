# Complete Experiment Report

- Status: `incomplete`
- Generated: `2026-08-17T02:44:09.981966+00:00`

## Matrix Experiments

| Experiment | Expected | Completed | Failed | Missing | Complete |
| --- | ---: | ---: | ---: | ---: | :---: |
| `published-ab` | 60 | 60 | 0 | 0 | yes |
| `model-probe-extended` | 1 | 0 | 1 | 0 | no |
| `secondary-model-probes` | 2 | 0 | 2 | 0 | no |

## Variant Metrics

### published-ab

| Variant | N | Eval Pass | Objective | Adherence | Required Enforcement | Under-Enforcement | Over-Optimization |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 30 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 0.8333 |
| `skill` | 30 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 0.1667 |

### model-probe-extended

| Variant | N | Eval Pass | Objective | Adherence | Required Enforcement | Under-Enforcement | Over-Optimization |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

Failures:
- `gpt-5.6-sol` / `baseline` / `fastapi-no-redis`: `execute`

### secondary-model-probes

| Variant | N | Eval Pass | Objective | Adherence | Required Enforcement | Under-Enforcement | Over-Optimization |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

Failures:
- `gpt-5.6-terra` / `baseline` / `fastapi-no-redis`: `execute`
- `gpt-5.5` / `baseline` / `fastapi-no-redis`: `execute`

## Runtime Experiments

| Experiment | Expected | Passed | Failed | Missing | Retries | Complete |
| --- | ---: | ---: | ---: | ---: | ---: | :---: |

## Protocol Conformance

| Protocol | Expected | Passed | Failed | Missing | Retry Rate | Average Retries | Complete |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `deterministic-v2-fixtures-v1` | 6 | 6 | 0 | 0 | 0.6667 | 0.8333 | yes |
| `runtime-validator-fixtures-v1` | 12 | 12 | 0 | 0 | 0.0000 | 0.0000 | yes |

## Gate Validator

- Cases: `32`
- Accuracy: `1.0`
- Precision: `1.0`
- Recall: `1.0`
- Failures: `0`

Missing and failed rows are never converted to zero-valued metric samples.
