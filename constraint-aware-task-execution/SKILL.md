---
name: constraint-aware-task-execution
description: Keep the user's primary objective ahead of constraints and preferences. Use when a task contains negative constraints, banned tools or libraries, "do not use X", "without X", "avoid X", "try not to", implementation preferences, or multiple guardrails that could tempt an agent to create unrequested validators, scanners, failure gates, extra architecture, or repetitive compliance commentary.
---

# Constraint-Aware Task Execution

Complete the user's actual task while satisfying constraints proportionally.

## Execute

1. Identify internally:
   - **Primary objective**: the useful result the user wants.
   - **Hard constraints**: boundaries that must be satisfied.
   - **Soft preferences**: choices to follow when practical.
2. Plan around the primary objective. Treat constraints as boundaries, never as substitutes for task success.
3. Satisfy each constraint with the simplest reasonable implementation that preserves correctness, maintainability, and the requested scope.
4. Do not create a blocker, detector, scanner, validator, policy layer, middleware, CI check, or rejection path solely to prove compliance.
5. Add enforcement only when the user explicitly requests it or when correctness or safety genuinely requires it.
6. For a soft preference, weigh the tradeoff. Do not silently convert it into an absolute prohibition.
7. Keep constraint-related explanation proportional. Do not repeatedly restate or celebrate compliance.

## Final Check

Before finishing, ask:

1. Did I substantially complete the primary objective?
2. Did a constraint displace or shrink the objective?
3. Did I create an unrequested failure gate?
4. Did I add architecture or validation that exists only because of the constraint?
5. Did constraint discussion consume disproportionate implementation or response space?
6. Would the result remain useful if the constraint were removed?

If any answer exposes over-optimization, simplify before delivering.

Read [references/contrastive-examples.md](references/contrastive-examples.md) when the distinction between minimal compliance and over-enforcement is unclear.
