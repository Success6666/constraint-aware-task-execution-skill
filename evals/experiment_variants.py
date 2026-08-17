"""Orthogonal prompt/execution variants used by the full experiment matrix."""

from __future__ import annotations

from dataclasses import dataclass


V1_CORE = """Complete the primary objective while treating hard constraints as boundaries and soft preferences as tradeoffs.
Use the simplest reasonable compliant implementation. Do not add a detector, scanner, policy layer, middleware, CI gate,
or rejection path solely to prove compliance. Add enforcement only when explicitly requested or required for safety."""

ANTI_OVEROPTIMIZATION = """Before answering, check that the primary objective is complete, no constraint displaced it,
and no architecture or validation exists only because a constraint was mentioned. Simplify any such addition."""

CONSTRAINT_ECHO = """Keep constraint commentary proportional. Demonstrate compliance through the result and do not
repeat the forbidden option unless a real tradeoff or explicit enforcement requirement needs explanation."""

POSITIVE_FRAMING = """Start from the useful result the user wants. Choose a suitable implementation inside the stated
boundaries and spend most of the answer on the implementation, verification, and operational decisions."""


@dataclass(frozen=True)
class Variant:
    name: str
    instruction: str = ""
    use_skill: bool = False
    structured_plan: bool = False
    validate_plan: bool = False
    repair_artifact: bool = False


VARIANTS = {
    "baseline": Variant("baseline"),
    "v1-full": Variant(
        "v1-full", "\n\n".join((V1_CORE, ANTI_OVEROPTIMIZATION, CONSTRAINT_ECHO)), use_skill=True,
    ),
    "remove-anti-overoptimization": Variant(
        "remove-anti-overoptimization", "\n\n".join((V1_CORE, CONSTRAINT_ECHO)), use_skill=True,
    ),
    "remove-constraint-echo": Variant(
        "remove-constraint-echo", "\n\n".join((V1_CORE, ANTI_OVEROPTIMIZATION)), use_skill=True,
    ),
    "positive-framing-only": Variant("positive-framing-only", POSITIVE_FRAMING),
    "structured-plan-only": Variant(
        "structured-plan-only", POSITIVE_FRAMING, structured_plan=True,
    ),
    "plan-validation": Variant(
        "plan-validation", POSITIVE_FRAMING, structured_plan=True, validate_plan=True,
    ),
    "full-v2": Variant(
        "full-v2",
        "\n\n".join((V1_CORE, ANTI_OVEROPTIMIZATION, CONSTRAINT_ECHO)),
        use_skill=True,
        structured_plan=True,
        validate_plan=True,
        repair_artifact=True,
    ),
}


def select_variants(names: list[str] | None) -> list[Variant]:
    if not names:
        return list(VARIANTS.values())
    missing = set(names) - set(VARIANTS)
    if missing:
        raise ValueError(f"Unknown variants: {', '.join(sorted(missing))}")
    ordered = list(dict.fromkeys(names))
    return [VARIANTS[name] for name in ordered]
