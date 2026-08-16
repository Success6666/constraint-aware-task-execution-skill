# Constraint-Aware Task Execution

An Agent Skill that keeps the user's primary objective ahead of negative constraints and soft preferences. It reduces the tendency to replace useful work with extra guards, scanners, policy layers, rejection paths, or repeated compliance commentary.

## Measured Result

The committed A/B run used `gpt-5.6-sol` with medium reasoning effort on 30 matched prompts: 15 English and 15 Chinese. The suite contains 12 hard-constraint cases, 6 soft-preference cases, 6 safety or explicit-enforcement cases, and 6 output or architecture-constraint cases.

| Metric | Baseline | Skill | Change |
| --- | ---: | ---: | ---: |
| Evaluation pass rate | 100% | 100% | unchanged |
| Objective coverage | 1.0000 | 1.0000 | unchanged |
| Constraint adherence | 1.0000 | 1.0000 | unchanged |
| Over-optimization score | 0.7333 | 0.1667 | **77.3% lower** |
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

`evals/run_ab.py` supports the original `baseline`/`skill` comparison plus `--variant ablation` for positive framing,
structured planning, plan validation, and full V2 prompt variants. `evals/ablation.py` summarizes completed rows while
keeping missing variants visible. Validation errors are machine feedback; over-optimization scores are evaluation-only
and are never included in model repair prompts.

The artifact validators prove observable contracts only. Unsupported semantic checks remain explicitly `unsupported`.

## Install

Use Node.js 22.20 or later. The commands below install a copied Skill globally and do not rely on symlinks.

Install for Codex, Claude Code, and OpenCode in one command:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-aware-task-execution-skill --skill constraint-aware-task-execution --agent codex --agent claude-code --agent opencode --global --copy --yes
```

Install for one agent:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-aware-task-execution-skill --skill constraint-aware-task-execution --agent codex --global --copy --yes
npx --yes skills@1.5.22 add Success6666/constraint-aware-task-execution-skill --skill constraint-aware-task-execution --agent claude-code --global --copy --yes
npx --yes skills@1.5.22 add Success6666/constraint-aware-task-execution-skill --skill constraint-aware-task-execution --agent opencode --global --copy --yes
```

For a project-local installation, run the same command from the project root and omit `--global`.

Verify repository discovery before installation:

```bash
npx --yes skills@1.5.22 add Success6666/constraint-aware-task-execution-skill --list
```

## Use

Invoke the Skill explicitly:

```text
Use $constraint-aware-task-execution to complete this task.
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
```

Each case uses an isolated temporary Git repository and a temporary `CODEX_HOME` containing only a copied authentication file. The authentication copy is deleted after the subprocess exits. Checkpoints are written atomically after each result, and one failed case does not discard completed results.

The deterministic scorer measures:

- objective coverage and hard constraint compliance;
- required safety or explicit enforcement;
- response-format and file-scope constraints;
- unrequested failure gates and constraint-only components;
- unnecessary constraint repetition and soft-preference hardening.

## Repository Layout

```text
skills/constraint-aware-task-execution/  Skill package
evals/cases.json                         30-case bilingual benchmark
evals/run_ab.py                          Isolated concurrent A/B runner
evals/scorer.py                          Deterministic scorer
evals/results/                           Raw responses, scores, and report
scripts/verify-install.py                Cross-agent installation verifier
tests/                                   Unit and distribution tests
```

## License

MIT
