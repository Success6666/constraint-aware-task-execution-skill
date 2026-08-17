"""Serializable execution state shared by matrix and artifact runtimes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


class Stage(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    VALIDATE = "validate"
    REPAIR = "repair"
    REPLAN = "replan"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class RetryBudget:
    transport: int = 1
    plan: int = 2
    artifact: int = 2

    def __post_init__(self) -> None:
        if self.transport < 1 or self.plan < 1 or self.artifact < 0:
            raise ValueError("transport and plan attempts must be positive; artifact attempts cannot be negative")


@dataclass
class AttemptRecord:
    stage: str
    index: int
    status: str
    started_at: str
    finished_at: str
    errors: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionState:
    run_id: str
    stage: str = Stage.PLAN.value
    attempts: list[AttemptRecord] = field(default_factory=list)
    passed_validators: dict[str, dict[str, Any]] = field(default_factory=dict)
    termination_reason: str | None = None
    completed: bool = False

    def count(self, *stages: str | Stage) -> int:
        wanted = {stage.value if isinstance(stage, Stage) else stage for stage in stages}
        return sum(item.stage in wanted for item in self.attempts)

    def can_attempt(self, stage: str | Stage, budget: RetryBudget) -> bool:
        value = stage.value if isinstance(stage, Stage) else stage
        limit = budget.plan if value in {Stage.PLAN.value, Stage.REPLAN.value} else budget.artifact
        return self.count(value) < limit

    def add_attempt(
        self,
        stage: str | Stage,
        status: str,
        *,
        errors: Iterable[str] = (),
        changed_paths: Iterable[str] = (),
        evidence: Mapping[str, Any] | None = None,
        started_at: str | None = None,
    ) -> AttemptRecord:
        value = stage.value if isinstance(stage, Stage) else stage
        now = datetime.now(timezone.utc).isoformat()
        record = AttemptRecord(
            value,
            self.count(value) + 1,
            status,
            started_at or now,
            now,
            list(errors),
            sorted(set(changed_paths)),
            dict(evidence or {}),
        )
        self.attempts.append(record)
        self.stage = value
        return record

    def update_validations(self, results: Iterable[Mapping[str, Any]]) -> None:
        for result in results:
            validator_id = str(result.get("id", result.get("type", result.get("validator", "unknown"))))
            if result.get("status") == "pass":
                self.passed_validators[validator_id] = dict(result)
            else:
                self.passed_validators.pop(validator_id, None)

    def finish(self, success: bool, reason: str) -> None:
        self.completed = success
        self.termination_reason = reason
        self.stage = Stage.COMPLETE.value if success else Stage.FAILED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "attempts": [asdict(item) for item in self.attempts],
            "passed_validators": self.passed_validators,
            "termination_reason": self.termination_reason,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionState":
        state = cls(str(value["run_id"]), str(value.get("stage", Stage.PLAN.value)))
        state.attempts = [AttemptRecord(**item) for item in value.get("attempts", [])]
        state.passed_validators = dict(value.get("passed_validators", {}))
        state.termination_reason = value.get("termination_reason")
        state.completed = bool(value.get("completed", False))
        return state


def write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def affected_validator_ids(specs: Iterable[Mapping[str, Any]], changed_paths: Iterable[str]) -> set[str]:
    changed = {str(path).replace("\\", "/") for path in changed_paths}
    affected: set[str] = set()
    for index, spec in enumerate(specs):
        validator_id = str(spec.get("id", f"{spec.get('type', 'unknown')}:{index}"))
        dependencies = {str(path).replace("\\", "/") for path in spec.get("paths", [])}
        single = spec.get("path")
        if single:
            dependencies.add(str(single).replace("\\", "/"))
        if not dependencies or dependencies & changed:
            affected.add(validator_id)
    return affected
