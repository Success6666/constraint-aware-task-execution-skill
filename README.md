# Constraint-Aware Task Execution Skill

A compact Codex/Agent Skill for reducing constraint over-optimization: the tendency to replace the user's primary objective with proof that a negative constraint was obeyed.

The Skill is based on the `Constraint-Aware Task Execution` prototype from the article *When Models Become Too Obedient: Constraint Over-Optimization and Agent Skill Design*.

## What It Prevents

- Turning "do not use X" into an unrequested global failure gate.
- Creating detectors, guards, scanners, policies, or middleware only to demonstrate compliance.
- Converting soft preferences into hard prohibitions.
- Spending more implementation and response space on a constraint than on the requested result.
- Repeating constraint compliance instead of demonstrating task completion.

## Install

Copy `constraint-aware-task-execution` into a Codex skill directory:

```text
~/.codex/skills/constraint-aware-task-execution
```

Invoke it explicitly with:

```text
Use $constraint-aware-task-execution to complete this task.
```

Its description also supports implicit activation for tasks containing negative constraints or soft preferences.

## Evaluation

The repository includes a reproducible A/B harness. It runs identical prompts in isolated workspaces:

- `baseline`: no project Skill is present.
- `skill`: the Skill is installed in the workspace and explicitly invoked.

The deterministic scorer measures objective coverage, unrequested failure gates, constraint-specific components, constraint echo, and soft-preference hardening.

```bash
python -m unittest discover -s tests -v
python evals/run_ab.py --variant both
python evals/run_ab.py --variant both --resume
python evals/rescore.py
python evals/build_report.py
```

Raw model responses and machine-readable scores are written under `evals/results/`.

## Current Result

The committed 12-pair run used `gpt-5.6-sol` at medium reasoning effort. See
[`evals/results/REPORT.md`](evals/results/REPORT.md) for aggregate metrics, per-case results,
and limitations. The result is a regression signal from one model/run, not a general claim
that the Skill improves every task or model.
