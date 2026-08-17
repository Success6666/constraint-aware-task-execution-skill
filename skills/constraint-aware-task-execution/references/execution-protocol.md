# Execution Protocol

Use the protocol only when the task needs a verifiable plan or artifact. Ordinary answers should remain single-pass.

## Stages

1. **Plan**: record the objective, independent requirements, hard constraints, soft preferences, artifacts, risks, and validation profile.
2. **Plan validation**: validate schema and explicit conflicts. Do not infer a gate from a negative preference.
3. **Execution**: complete the entire objective using the validated plan as internal context.
4. **Artifact validation**: run only allowlisted validators derived from the user request or benchmark contract.
5. **Targeted repair**: return machine error codes for the affected artifact, then re-run all applicable validators.
6. **Replan**: use only for structural plan failures. Stop after the configured budget.

## Constraint policy

- Hard constraints are mandatory boundaries.
- Enforcement constraints are hard constraints with an explicit or safety-required failure action.
- Soft preferences are tradeoffs and may be suppressed when they conflict with hard constraints.
- Two contradictory hard constraints require clarification or an explicit failure state.
- An unknown or ambiguous classification must not be promoted to enforcement.

## Result states

- `pass`: the observable contract was checked and satisfied.
- `fail`: the observable contract was checked and violated.
- `unsupported`: no deterministic validator is available; this is not a pass.
- `partial`: only declared dimensions were measured; broader semantic quality remains unproven.

Keep model transport retries separate from plan and artifact retries. Persist every attempt, error code, termination reason, and usage record without exposing credentials.

