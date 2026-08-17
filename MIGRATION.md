# Migration to 0.4

## Version status

`0.4.0-rc.7` is the stabilized cross-model planning candidate. Keep `v0.3.0` and earlier release results immutable and write new experiments under a distinct `--experiment` directory. Promote to `0.4.0` only after the complete model matrices and blinded semantic review satisfy the task acceptance gates.

## Skill rename

The current Skill name is `constraint-exec`. Replace the former `constraint-aware-task-execution` directory and invocation with `skills/constraint-exec` and `$constraint-exec`. Remove the former installed copy after the new Skill is discoverable so two descriptions do not compete for the same request.

## Runner changes

- `evals/run_matrix.py` accepts `--executor codex|ollama` and `--transport-attempts`.
- Transport retries are recorded separately from plan retries and artifact repairs.
- Result signatures include executor and transport configuration; incompatible resume rows are regenerated.
- `unsupported` no longer implies `artifact_contract_pass=true`.
- `evals/run_runtime.py` uses the same executor contract, state records, transport accounting, and validator registry.

## Protocol changes

- Execution plans may declare `target`, `scope`, `polarity`, and `priority`.
- Enforcement constraints require an explicit gate and failure action.
- Hard conflicts invalidate a plan; a conflicting soft preference is suppressed and reported.
- Unknown validators and unavailable optional runtimes return `unsupported`.
- Runtime commands must match the fixed allowlist and execute without a shell.

## Integration changes

- Use `evals/agent_runtime.py` for a single versioned generation request.
- Use `scripts/execute_protocol.py` for plan, execution, validation, and bounded repair.
- Validate integrations against `evals/schemas/runtime-request.schema.json`, `evals/schemas/result.schema.json`, and `evals/schemas/score.schema.json`.
- Validate one-shot generation integrations against `evals/schemas/generation-request.schema.json` and `evals/schemas/generation-response.schema.json`.
- Use `evals/benchmark-manifest.json` for pairing keys, missing-sample policy, and release gates.
- Validate the frozen manifest against `evals/schemas/benchmark-manifest.schema.json` and verify every declared dataset digest before a release run.
- Use `evals/pairwise_review.py` to create blinded semantic review packets and apply completed review scores without exposing variant labels to reviewers.

## Capability gate

Final candidates must have complete baseline pairing and zero capability regressions for observable dimensions. Functional, non-constraint requirement, artifact, format, path, or declared-quality loss blocks promotion even when over-optimization improves. Efficiency ratios remain separate diagnostics.

## Runtime trust boundary

The JSON runtime is intended for trusted local operators and CI, not direct exposure to untrusted clients. The caller must constrain workspace and output roots, pass only required environment entries, and keep credentials outside generated workspaces. Redaction reduces accidental persistence but does not make arbitrary requests or inherited secrets safe.
