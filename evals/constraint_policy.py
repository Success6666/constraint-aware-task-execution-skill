"""Normalize constraints and resolve only explicit, machine-visible conflicts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from language_rules import classify_statement


KINDS = {"hard", "soft", "enforcement"}
POLARITIES = {"require", "forbid", "prefer", "avoid"}
OPPOSITES = {("require", "forbid"), ("forbid", "require"), ("prefer", "avoid"), ("avoid", "prefer")}


@dataclass(frozen=True)
class Constraint:
    id: str
    kind: str
    statement: str
    target: str
    scope: str
    polarity: str
    required_gate: bool
    priority: int


@dataclass(frozen=True)
class PolicyIssue:
    code: str
    path: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class PolicyEvaluation:
    constraints: tuple[Constraint, ...]
    issues: tuple[PolicyIssue, ...]
    suppressed: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "constraints": [item.__dict__ for item in self.constraints],
            "issues": [item.to_dict() for item in self.issues],
            "suppressed": list(self.suppressed),
        }


def _normalize(entry: Mapping[str, Any], path: str, default_kind: str) -> tuple[Constraint | None, list[PolicyIssue]]:
    issues: list[PolicyIssue] = []
    statement = str(entry.get("statement", entry.get("preference", ""))).strip()
    kind = str(entry.get("type", default_kind)).strip().casefold()
    inferred = classify_statement(statement)
    polarity = str(entry.get("polarity", inferred.polarity)).strip().casefold()
    target = str(entry.get("target", "")).strip().casefold()
    scope = str(entry.get("scope", "global")).strip().casefold() or "global"
    identifier = str(entry.get("id", path)).strip() or path
    required_gate = bool(entry.get("required_gate", entry.get("enforcement_required", False)))
    priority_value = entry.get("priority", 100 if kind in {"hard", "enforcement"} else 10)
    try:
        priority = int(priority_value)
    except (TypeError, ValueError):
        priority = 0
        issues.append(PolicyIssue("CONSTRAINT_PRIORITY", f"{path}.priority", "error", "priority must be an integer"))
    if kind not in KINDS:
        issues.append(PolicyIssue("CONSTRAINT_KIND", f"{path}.type", "error", "unsupported constraint kind"))
    if polarity not in POLARITIES:
        polarity = "unknown"
        issues.append(PolicyIssue("CONSTRAINT_POLARITY_UNKNOWN", f"{path}.polarity", "warning", "polarity is not explicit"))
    if kind == "enforcement" and not required_gate:
        issues.append(PolicyIssue("ENFORCEMENT_GATE_REQUIRED", f"{path}.required_gate", "error", "enforcement constraints require a gate"))
    if required_gate and kind != "enforcement":
        kind = "enforcement"
    if not statement:
        return None, issues
    return Constraint(identifier, kind, statement, target, scope, polarity, required_gate, priority), issues


def evaluate_constraint_policy(
    hard_constraints: Sequence[Mapping[str, Any]],
    soft_preferences: Sequence[Mapping[str, Any]],
) -> PolicyEvaluation:
    normalized: list[Constraint] = []
    issues: list[PolicyIssue] = []
    for index, entry in enumerate(hard_constraints):
        if isinstance(entry, Mapping):
            item, found = _normalize(entry, f"hard_constraints[{index}]", "hard")
            issues.extend(found)
            if item:
                normalized.append(item)
    for index, entry in enumerate(soft_preferences):
        if isinstance(entry, Mapping):
            item, found = _normalize(entry, f"soft_preferences[{index}]", "soft")
            issues.extend(found)
            if item:
                normalized.append(item)

    suppressed: set[str] = set()
    for left_index, left in enumerate(normalized):
        if not left.target or left.polarity == "unknown":
            continue
        for right in normalized[left_index + 1:]:
            if (left.target, left.scope) != (right.target, right.scope):
                continue
            if (left.polarity, right.polarity) not in OPPOSITES:
                continue
            left_hard = left.kind in {"hard", "enforcement"}
            right_hard = right.kind in {"hard", "enforcement"}
            if left_hard and right_hard:
                issues.append(PolicyIssue("HARD_CONSTRAINT_CONFLICT", left.id, "error", f"conflicts with {right.id}"))
            elif left_hard != right_hard:
                loser = right if left_hard else left
                suppressed.add(loser.id)
                issues.append(PolicyIssue("SOFT_PREFERENCE_SUPPRESSED", loser.id, "warning", "conflicts with a hard constraint"))
            else:
                loser = min((left, right), key=lambda item: (item.priority, item.id))
                suppressed.add(loser.id)
                issues.append(PolicyIssue("SOFT_PREFERENCE_CONFLICT", loser.id, "warning", "lower-priority preference suppressed"))
    return PolicyEvaluation(tuple(normalized), tuple(issues), tuple(sorted(suppressed)))

