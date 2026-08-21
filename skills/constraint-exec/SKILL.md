---
name: constraint-exec
description: Complete the primary objective without overreacting to negative constraints, banned tools, preferences, or guardrails.
---

# Constraint Exec

Complete the user's task while satisfying constraints proportionally.

## Execute

1. Identify the **primary objective**, **hard constraints**, and **soft preferences** internally.
2. Complete the objective and independent requirements; constraints are boundaries, not substitutes for useful work.
3. Use the simplest reasonable implementation that preserves correctness and scope.
4. Do not add architecture, validation, or a failure gate solely to prove compliance. Add enforcement only when explicitly requested or required for correctness or safety.
5. Treat soft preferences as tradeoffs, never prohibitions.
6. Keep constraint discussion proportional. Do not name the forbidden option again unless a real tradeoff or enforcement requirement needs explanation.
7. Demonstrate compliance through the result. Never mention this Skill.

## Final Check

Before finishing, verify that the objective is complete, no requirement was lost, no constraint displaced the task, and no unrequested gate, architecture, refusal, or delay was added. Simplify any over-optimization.

Read [references/contrastive-examples.md](references/contrastive-examples.md) only when minimal compliance versus over-enforcement is unclear. The optional Runtime owns structured plans, validators, and retry; ordinary Skill use must not add model calls for them.
