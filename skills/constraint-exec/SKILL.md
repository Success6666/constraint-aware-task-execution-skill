---
name: constraint-exec
description: Keep the user's primary objective ahead of constraints and preferences. Use when a task contains negative constraints, banned tools or libraries, "do not use X", "without X", "avoid X", "try not to", implementation preferences, or multiple guardrails that could tempt an agent to create unrequested validators, scanners, failure gates, extra architecture, or repetitive compliance commentary.
---

# Constraint Exec

Complete the user's actual task while satisfying constraints proportionally.

## Execute

1. Identify internally:
   - **Primary objective**: the useful result the user wants.
   - **Hard constraints**: boundaries that must be satisfied.
   - **Soft preferences**: choices to follow when practical.
2. Plan around the primary objective. Treat constraints as boundaries, never as substitutes for task success.
   Preserve every independent requirement that contributes to usefulness; a shorter or easier task is not an equivalent result.
3. Satisfy each constraint with the simplest reasonable implementation that preserves correctness, maintainability, and the requested scope.
4. Do not create a blocker, detector, scanner, validator, policy layer, middleware, CI check, or rejection path solely to prove compliance.
5. Add enforcement only when the user explicitly requests it or when correctness or safety genuinely requires it.
6. For a soft preference, weigh the tradeoff. Do not silently convert it into an absolute prohibition.
   If a soft preference conflicts with a hard constraint, follow the hard constraint and suppress only the conflicting preference.
7. Keep constraint-related explanation proportional. After choosing a compliant path, do not name the forbidden option again unless the comparison is needed to explain a real tradeoff, risk, or explicit enforcement request.
8. If the result is clear without constraint commentary, demonstrate compliance through the implementation and omit the commentary.
9. Never mention this Skill, its rules, or that the Skill was loaded or followed.

## Structured execution

For tasks that need a plan or a verifiable artifact, keep these concepts separate:

- `constraint`: the boundary stated by the user;
- `implementation_strategy`: the simplest compliant approach;
- `failure_gate`: an explicit rejection or blocking action, used only when the user requests enforcement or safety requires it.

When an external execution harness is available, prefer a structured plan and deterministic checks for JSON shape,
file scope, syntax, compilation, tests, and other observable contracts. Do not claim semantic success when the
artifact cannot be deterministically verified. Return machine-readable validation errors and repair only the affected
section before rebuilding the artifact or replanning.

Use this order:

1. Preserve the objective and non-constraint requirements in the plan.
   Record independent requirements separately from constraints and give each an acceptance criterion; do not hide them inside a broad objective sentence.
2. Validate plan structure and explicit hard/enforcement conflicts.
3. Execute the whole task.
4. Validate only allowlisted, observable contracts. Treat unknown checks as `unsupported`, never as passed.
5. Repair the affected artifact within a finite budget, then re-run all applicable contracts.
6. Replan only for structural plan failures; stop with an explicit failure state when the budget is exhausted.

Do not use evaluation scores, output length, or constraint-related word counts as repair feedback.

## Final Check

Before finishing, ask:

1. Did I substantially complete the primary objective?
2. Did a constraint displace or shrink the objective?
3. Did I create an unrequested failure gate?
4. Did I add architecture or validation that exists only because of the constraint?
5. Did constraint discussion consume disproportionate implementation or response space?
6. Would the result remain useful if the constraint were removed?
7. Did I omit a requested capability, necessary detail, or working artifact to make compliance easier?
8. Did I refuse, ask for clarification, or add caution when the task was already actionable and safe?

If any answer exposes over-optimization, simplify before delivering.

Read [references/contrastive-examples.md](references/contrastive-examples.md) when the distinction between minimal compliance and over-enforcement is unclear.
For runtime integration, read [references/execution-protocol.md](references/execution-protocol.md),
[references/validation-contracts.md](references/validation-contracts.md), and
[references/capability-preservation.md](references/capability-preservation.md).
