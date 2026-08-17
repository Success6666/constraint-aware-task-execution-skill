# Constraint Exec

An Agent Skill that keeps the user's primary objective ahead of negative constraints and soft preferences. It reduces the tendency to replace useful work with extra guards, scanners, policy layers, rejection paths, or repeated compliance commentary.

Current repository version: `0.4.0-rc.6`. This prerelease adds grounded minimal plan fallback telemetry and strict capability-retention detection; final promotion remains gated by the full model matrix and blinded semantic review.

## Measured Result

The committed A/B run used `gpt-5.6-sol` with medium reasoning effort on 30 matched prompts: 15 English and 15 Chinese. The suite contains 12 hard-constraint cases, 6 soft-preference cases, 6 safety or explicit-enforcement cases, and 6 output or architecture-constraint cases.

| Metric | Baseline | Skill | Change |
| --- | ---: | ---: | ---: |
| Evaluation pass rate | 100% | 100% | unchanged |
| Objective coverage | 1.0000 | 1.0000 | unchanged |
| Constraint adherence | 1.0000 | 1.0000 | unchanged |
| Over-optimization score | 0.8333 | 0.1667 | **80.0% lower** |
| Unnecessary constraint echo | 0.4333 | 0.1667 | **61.5% lower** |
| Constraint-only components | 0.1000 | 0.0000 | **100% lower** |

Per case, 7 improved, 0 worsened, and 23 tied. The scorer compares only responses that pass objective and constraint gates. Required safety enforcement is measured for compliance but is not counted as unnecessary constraint repetition.

See [`evals/results/REPORT.md`](evals/results/REPORT.md), [`evals/results/summary.json`](evals/results/summary.json), and the committed raw responses for the full evidence. This is a deterministic, single-model regression experiment, not a universal performance claim or a statistical significance test.

## V1.5/V2 execution protocol

The repository now includes deterministic execution primitives in [`evals/protocol.py`](evals/protocol.py):

- versioned structured-plan parsing and validation;
- separate constraint, implementation strategy, and failure-gate fields;
- relation-based gate detection with negation and quoted-example handling;
- plugin validators for JSON, Markdown, file scope, and Python AST/compile checks;
- explicit `pass`, `fail`, and `unsupported` artifact states;
- bounded Level 1 artifact repair, Level 2 artifact regeneration, and Level 3 replanning decisions.

`evals/run_ab.py` preserves the released `baseline`/`skill` comparison. `evals/run_matrix.py` runs eight orthogonal
variants: baseline, full V1, two V1 rule removals, positive framing, structured planning, plan validation, and full V2.
Structured variants use separate plan and execution calls; full V2 adds deterministic artifact validation and bounded
repair. `evals/run_runtime.py` validates actual workspace artifacts with allowlisted validators and tests.

`evals/run_protocol_fixtures.py`, `evals/run_runtime_fixtures.py`, and `evals/evaluate_validators.py` provide deterministic
conformance evidence. `evals/build_experiment_report.py` reports failed and missing rows explicitly instead of converting
them into zero-valued metric samples. See [`evals/experiments/EXPERIMENT_STATUS.md`](evals/experiments/EXPERIMENT_STATUS.md)
for the current completion state. Validation errors are machine feedback; over-optimization scores are evaluation-only
and are never included in model repair prompts.

The artifact validators prove observable contracts only. Unsupported semantic checks remain explicitly `unsupported`.

## Capability retention

Constraint compliance is not accepted when it makes the model less useful. The evaluation layer pairs each candidate with the same case, model, repeat, and sampling signature, then reports:

- objective, hard-constraint, enforcement, format, path, and artifact retention;
- non-constraint requirement and declared quality retention;
- unnecessary refusal, clarification, and excessive-caution regressions;
- token and latency ratios as separate efficiency metrics.

Missing or failed pairs are coverage gaps, not zero scores. Output length and keyword counts do not prove quality. A final candidate with a functional, required-content, artifact, or declared-quality regression fails the release gate even if its over-optimization score improves.

For semantic quality that deterministic contracts cannot establish, `evals/pairwise_review.py` prepares anonymized baseline/candidate review packets and separate mapping keys. Final release evidence requires two independent reviewers per pair; disputed pairs require adjudication. Reviewers score correctness, completeness, usefulness, and requirement retention on both sides instead of inferring quality from keywords.

## Model-independent runtime

`evals/executors/` provides Codex CLI and local Ollama adapters with common request, result, capability, usage, and failure contracts. `scripts/execute_protocol.py` exposes the plan, execute, validate, and bounded-repair flow through versioned JSON input and output. Workspace writes require an executor that declares workspace access; unsupported capabilities terminate explicitly.

Runtime validators are selected from a fixed registry. File access is contained to the workspace, commands must match an allowlist and run without a shell, and unknown validators return `unsupported`. Trace and result persistence redact credential-shaped values and use isolated temporary runtime state.

The runtime is an operator-controlled local interface, not a remote sandbox for untrusted JSON. Only load request files from trusted automation. Constrain workspace and output roots before invocation, pass the minimum environment needed by the executor, keep credentials outside the workspace, and treat redaction as defense in depth rather than secret storage.

## Install

Use Node.js 22.20 or later. The commands below install a copied Skill globally and do not rely on symlinks.

Install for Codex, Claude Code, and OpenCode in one command:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-exec --skill constraint-exec --agent codex --agent claude-code --agent opencode --global --copy --yes
```

Install for one agent:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-exec --skill constraint-exec --agent codex --global --copy --yes
npx --yes skills@1.5.22 add Success6666/constraint-exec --skill constraint-exec --agent claude-code --global --copy --yes
npx --yes skills@1.5.22 add Success6666/constraint-exec --skill constraint-exec --agent opencode --global --copy --yes
```

For a project-local installation, run the same command from the project root and omit `--global`.

Verify repository discovery before installation:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-exec --list
```

## Use

Invoke the Skill explicitly:

```text
Use $constraint-exec to complete this task.
```

Its description also supports automatic discovery for requests containing negative constraints, banned tools, soft preferences, or multiple guardrails.

## What It Changes

- Keeps the requested deliverable as the success criterion.
- Treats hard constraints as boundaries and soft preferences as tradeoffs.
- Avoids unrequested detectors, scanners, middleware, CI gates, and rejection paths.
- Preserves enforcement when the user explicitly requests it or safety requires it.
- Demonstrates compliance through the result instead of repeatedly discussing the constraint.
- Avoids mentioning the Skill or its internal instructions in the final answer.

## Evaluation

Run local validation:

```bash
python -m unittest discover -s tests -v
python -m compileall -q evals scripts tests
python scripts/verify-install.py
```

Run or resume the A/B evaluation with an authenticated Codex CLI:

```bash
python evals/run_ab.py --variant both --model gpt-5.6-sol --reasoning-effort medium --jobs 3
python evals/run_ab.py --variant both --resume --model gpt-5.6-sol --reasoning-effort medium --jobs 3
python evals/rescore.py
python evals/build_report.py
python evals/evaluate_validators.py --output evals/experiments/validator-v1/results.json
python evals/run_protocol_fixtures.py
python evals/run_runtime_fixtures.py
```

Run an isolated orthogonal matrix or workspace-artifact experiment:

```bash
python evals/run_matrix.py --experiment full-matrix --model MODEL --variant baseline --variant full-v2
python evals/run_matrix.py --experiment local-matrix --executor ollama --model qwen3.5:9b --variant baseline --variant full-v2
python evals/run_runtime.py --experiment runtime-matrix --model MODEL --mode direct --mode full-v2
python evals/pairwise_review.py prepare --results RESULTS.json --experiment-root EXPERIMENT_DIR --variant full-v2 --seed-file reviewer-1.seed --reviewer-id reviewer-1 --cases evals/cases.json --reviews reviewer-1.json --key reviewer-1.key.json
python evals/pairwise_review.py prepare --results RESULTS.json --experiment-root EXPERIMENT_DIR --variant full-v2 --seed-file reviewer-2.seed --reviewer-id reviewer-2 --cases evals/cases.json --reviews reviewer-2.json --key reviewer-2.key.json
python evals/pairwise_review.py apply --results RESULTS.json --reviews reviewer-1.json --key reviewer-1.key.json --reviews reviewer-2.json --key reviewer-2.key.json --minimum-reviewers 2 --output RESULTS.reviewed.json
python evals/release_experiment.py --dry-run --allow-dirty --allow-untagged
```

Run the stable Agent Runtime interface:

```bash
python evals/agent_runtime.py --describe
python evals/agent_runtime.py --request generation-request.json --response generation-response.json
python scripts/execute_protocol.py request.json --workspace-root WORKSPACE_ROOT --artifact-root ARTIFACT_ROOT --output result.json
```

The first interface performs one model generation with normalized capabilities and failures. The second executes the full plan, validate, and bounded-repair protocol.

The full execution request and result contracts are defined in `evals/schemas/runtime-request.schema.json` and `evals/schemas/result.schema.json`. The single-generation interface uses `evals/schemas/generation-request.schema.json` and `evals/schemas/generation-response.schema.json`. Benchmark pairing, frozen datasets, required matrices, semantic review coverage, and release gates are declared in `evals/benchmark-manifest.json`; the benchmark and release configuration have dedicated schemas under `evals/schemas/`.

Each case uses an isolated temporary Git repository and a temporary `CODEX_HOME` containing only a copied authentication file. The authentication copy is deleted after the subprocess exits. Checkpoints are written atomically after each result, and one failed case does not discard completed results.

Release runs first verify frozen dataset hashes, JSON schemas, Skill metadata, and source secret patterns. Resume is valid only when the case, executor, model, sampling configuration, variant, validator profile, and budgets match the saved signature. Human review mapping keys and seeds stay separate from blinded review packets and generated outputs.

The deterministic scorer measures:

- objective coverage and hard constraint compliance;
- required safety or explicit enforcement;
- response-format and file-scope constraints;
- unrequested failure gates and constraint-only components;
- unnecessary constraint repetition and soft-preference hardening.

## Repository Layout

```text
skills/constraint-exec/                  Skill package
evals/cases.json                         30-case bilingual benchmark
evals/run_ab.py                          Isolated concurrent A/B runner
evals/run_matrix.py                      Orthogonal plan/execute/repair matrix runner
evals/run_runtime.py                     Real workspace artifact runner
evals/executors/                         Model execution adapters
evals/validators/                        Allowlisted artifact validator registry
evals/capability_metrics.py              Paired capability-retention metrics
evals/pairwise_review.py                 Blinded semantic capability review workflow
evals/scorer.py                          Deterministic scorer
evals/build_experiment_report.py         Strict completeness and metric report
evals/results/                           Raw responses, scores, and report
scripts/execute_protocol.py              Versioned Agent Runtime JSON CLI
scripts/verify-install.py                Cross-agent installation verifier
tests/                                   Unit and distribution tests
```

## License

MIT
