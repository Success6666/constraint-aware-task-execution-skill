# Changelog

## 0.4.0-rc.7 - 2026-08-17

- Bounded release matrix concurrency to reduce wall-clock time while retaining provider-level call limits and resumable checkpoints.

## 0.4.0-rc.6 - 2026-08-17

- Added a grounded minimal plan fallback that can only use frozen case metadata and records every synthesized field.
- Removed model-invented hard constraints when a case declares a soft-only preference, using path-scoped deterministic normalization.
- Added plan-fallback telemetry to matrix reports and made candidate-only fallback a capability-regression signal.
- Added regression tests for fallback grounding, soft-preference preservation, and report visibility.
- Verified 95 unit tests, compilation, and representative Ollama safety/soft-preference runs.

## 0.4.0-rc.5 - 2026-08-17

- Added deterministic path-scoped normalization for validated plan classification errors.
- Reclassified only reported unrequested enforcement constraints and removed only reported ungrounded constraints.
- Preserved raw model plans and recorded normalized plans, changes, and second-pass validation evidence separately.
- Applied the same normalization contract to answer matrices and real workspace Runtime execution.
- Fixed local-model full-v2 plan exhaustion without increasing retry budgets or weakening validation.
- Verified the fix on two independent Ollama full-v2 cases and 91 repository tests.

## 0.4.0-rc.4 - 2026-08-17

- Replaced writable model workspaces with a strict read-only artifact-bundle generation protocol.
- Added validated atomic file application with path, symlink, duplicate, encoding, file-count, and size boundaries.
- Bound Runtime resume evidence to protocol, Skill, runner, and artifact-schema digests.
- Required command validators to observe actual test execution instead of accepting zero-test discovery.
- Clarified Python runtime contracts to require standard-library `unittest.TestCase` coverage.
- Preserved explicitly requested safety gates while rejecting unrequested enforcement classifications during plan repair.
- Completed 12/12 real workspace runtime cases across direct and full-v2 execution.
- Added scoreable adversarial answer cases to the primary release matrix and blinded review packets.
- Added release-coverage preflight checks for dataset bindings, matrix dimensions, model counts, and semantic-review policy.

## 0.4.0-rc.3 - 2026-08-17

- Replaced the local-model shell adapter with a bounded Ollama HTTP executor and normalized usage evidence.
- Preserved only the active Codex provider route in isolated execution homes and added serialized scheduling, cooldown, and recoverable backoff.
- Added a strict provider-compatible execution-plan output schema while retaining canonical deterministic validation.
- Added request-grounded plan validation for requirements, constraints, preferences, and explicit enforcement gates.
- Made plan retries preserve complete plans and repair only reported classification or structural errors.
- Added paired capability-retention gates that distinguish functional regressions from latency and token cost.
- Fixed negated backend phrasing in the scorer and retained failed-contract rows when usable score evidence exists.
- Verified representative Codex and Ollama pairs without observed capability regression before the full release matrix.

## 0.4.0-rc.2 - 2026-08-17

- Renamed the Skill and invocation to `constraint-exec`.
- Added a frozen release experiment manifest with dataset integrity metadata and required model matrices.
- Tightened runtime, result, score, plan, review, and benchmark JSON schemas.
- Documented the local runtime trust boundary, minimal-environment rule, evidence provenance, and semantic review coverage gate.
- Added ignore rules for local credentials, runtime checkpoints, review mapping keys, and transient model artifacts.
- Added a release preflight for dataset hashes, schema contracts, Skill metadata, and source secret scanning.
- Added trusted workspace/artifact roots, minimal subprocess environment inheritance, versioned full checkpoints, and strict canonical plan parsing for new Runtime requests.
- Added two-reviewer blinded aggregation, disagreement adjudication, and release-orchestrated review pause/resume.
- Renamed the GitHub repository and local `origin` to `Success6666/constraint-exec`.

## 0.4.0-rc.1 - 2026-08-17

- Added model-independent Codex CLI and local Ollama executor contracts.
- Added a versioned Agent Runtime JSON interface with finite plan and artifact retry budgets.
- Added allowlisted artifact validators, path containment, static checks, and explicit unsupported states.
- Added multilingual constraint classification and hard, soft, and enforcement conflict policy.
- Added paired capability-retention metrics and a release gate that blocks functional or quality regressions.
- Added anonymized pairwise semantic review preparation and score application for evidence that deterministic contracts cannot establish.
- Added runtime environment isolation, credential cleanup, output redaction, benchmark manifest, and result schemas.

This prerelease contains the complete non-test implementation surface. Final `0.4.0` promotion requires the deferred validation and experiment matrix.
