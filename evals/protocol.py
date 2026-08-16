"""Deterministic execution protocol primitives for the V2 evaluation harness.

The module deliberately reports machine-verifiable states only. It does not
judge semantic quality that cannot be established from the supplied artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ast
import json
from pathlib import PurePosixPath
import re
from typing import Any, Callable, Mapping


PLAN_SCHEMA_VERSION = "1.0"
ALLOWED_CONSTRAINT_TYPES = {"hard", "soft", "enforcement"}
ALLOWED_ARTIFACT_KINDS = {"text", "json", "markdown", "python", "file"}


@dataclass(frozen=True)
class PlanIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class PlanValidation:
    valid: bool
    issues: tuple[PlanIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.__dict__ for issue in self.issues],
        }


@dataclass(frozen=True)
class ArtifactValidation:
    status: str
    validator: str
    errors: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "validator": self.validator,
            "errors": list(self.errors),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RetryDecision:
    level: str
    attempt: int
    max_attempts: int
    reason: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _issue(code: str, path: str, message: str) -> PlanIssue:
    return PlanIssue(code, path, message)


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def detect_gate_relation(text: str, targets: list[str] | None = None) -> bool:
    """Detect a high-confidence target + mechanism + failure relation."""
    normalized = text.casefold()
    normalized = re.sub(r"[`'\"]([^`'\"]+)[`'\"]", " ", normalized)
    negated = (
        r"\b(?:do not|don't|never|must not|should not)\b[^.!?\n]{0,180}"
        r"(?:detector|scanner|guard|validator|policy|middleware|hook|fail|reject|block)",
        r"(?:不|不要|不得|无需|避免)[^。！？\n]{0,100}"
        r"(?:检测器|扫描器|守卫|校验器|策略|中间件|检查器|失败|拒绝|阻止|禁止|拦截|隔离)",
    )
    for pattern in negated:
        normalized = re.sub(pattern, " ", normalized, flags=re.DOTALL)

    mechanism = r"(?:detector|scanner|guard|validator|policy|middleware|hook|检测器|扫描器|守卫|校验器|策略|中间件|检查器)"
    failure = r"(?:fail|reject|block|forbid|ban|quarantine|拒绝|阻止|失败|禁止|拦截|隔离)"
    target_pattern = None
    if targets:
        target_pattern = "(?:" + "|".join(re.escape(target.casefold()) for target in targets) + ")"
    for sentence in re.split(r"[.!?。！？\n]+", normalized):
        if not (re.search(mechanism, sentence) and re.search(failure, sentence)):
            continue
        if target_pattern is None or re.search(target_pattern, sentence):
            return True
    return False


def validate_plan(plan: Mapping[str, Any]) -> PlanValidation:
    issues: list[PlanIssue] = []
    if not isinstance(plan, Mapping):
        return PlanValidation(False, (_issue("PLAN_NOT_OBJECT", "$", "plan must be an object"),))

    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        issues.append(_issue("SCHEMA_VERSION", "schema_version", f"expected {PLAN_SCHEMA_VERSION}"))
    if not _is_nonempty_string(plan.get("objective")):
        issues.append(_issue("OBJECTIVE_REQUIRED", "objective", "objective must be non-empty"))

    constraints = plan.get("hard_constraints", [])
    if not isinstance(constraints, list):
        issues.append(_issue("CONSTRAINTS_TYPE", "hard_constraints", "must be an array"))
        constraints = []
    for index, constraint in enumerate(constraints):
        path = f"hard_constraints[{index}]"
        if not isinstance(constraint, Mapping):
            issues.append(_issue("CONSTRAINT_NOT_OBJECT", path, "constraint must be an object"))
            continue
        statement = constraint.get("statement", constraint.get("constraint"))
        strategy = constraint.get("strategy", constraint.get("implementation_strategy"))
        required_gate = constraint.get("required_gate", constraint.get("enforcement_required", False))
        if not _is_nonempty_string(statement):
            issues.append(_issue("CONSTRAINT_FIELD_REQUIRED", f"{path}.statement", "must be non-empty"))
        if not _is_nonempty_string(strategy):
            issues.append(_issue("CONSTRAINT_FIELD_REQUIRED", f"{path}.strategy", "must be non-empty"))
        if constraint.get("type") is not None and constraint.get("type") not in ALLOWED_CONSTRAINT_TYPES:
            issues.append(_issue("CONSTRAINT_TYPE", f"{path}.type", "unsupported constraint type"))
        if required_gate not in (True, False):
            issues.append(_issue("ENFORCEMENT_REQUIRED_TYPE", f"{path}.enforcement_required", "must be boolean"))
        gate_text = " ".join(str(value or "") for value in (strategy, constraint.get("failure_action")))
        if not required_gate and detect_gate_relation(gate_text):
            issues.append(_issue(
                "UNREQUESTED_FAILURE_GATE",
                path,
                "strategy creates a target/enforcement/failure gate without an explicit requirement",
            ))
        if required_gate and not _is_nonempty_string(constraint.get("failure_action")):
            issues.append(_issue("FAILURE_ACTION_REQUIRED", f"{path}.failure_action", "required for enforcement constraints"))

    preferences = plan.get("soft_preferences", [])
    if not isinstance(preferences, list):
        issues.append(_issue("PREFERENCES_TYPE", "soft_preferences", "must be an array"))
        preferences = []
    for index, preference in enumerate(preferences):
        path = f"soft_preferences[{index}]"
        if not isinstance(preference, Mapping):
            issues.append(_issue("PREFERENCE_NOT_OBJECT", path, "preference must be an object"))
            continue
        if not _is_nonempty_string(preference.get("preference")):
            issues.append(_issue("PREFERENCE_REQUIRED", f"{path}.preference", "must be non-empty"))
        if not _is_nonempty_string(preference.get("tradeoff")):
            issues.append(_issue("TRADEOFF_REQUIRED", f"{path}.tradeoff", "must be non-empty"))

    for key in ("risk_points", "artifacts"):
        if not isinstance(plan.get(key, []), list):
            issues.append(_issue("FIELD_TYPE", key, "must be an array"))
    for index, artifact in enumerate(plan.get("artifacts", [])):
        path = f"artifacts[{index}]"
        if not isinstance(artifact, Mapping):
            issues.append(_issue("ARTIFACT_NOT_OBJECT", path, "artifact must be an object"))
            continue
        if not _is_nonempty_string(artifact.get("path")):
            issues.append(_issue("ARTIFACT_PATH_REQUIRED", f"{path}.path", "must be non-empty"))
        if artifact.get("kind") not in ALLOWED_ARTIFACT_KINDS:
            issues.append(_issue("ARTIFACT_KIND", f"{path}.kind", "unsupported artifact kind"))

    profile = plan.get("validation_profile", {})
    if not isinstance(profile, Mapping):
        issues.append(_issue("VALIDATION_PROFILE_TYPE", "validation_profile", "must be an object"))
    else:
        validators = profile.get("validators", [])
        if not isinstance(validators, list):
            issues.append(_issue("VALIDATORS_TYPE", "validation_profile.validators", "must be an array"))
        else:
            for index, validator in enumerate(validators):
                if not isinstance(validator, Mapping) or not _is_nonempty_string(validator.get("type")):
                    issues.append(_issue("VALIDATOR_REQUIRED", f"validation_profile.validators[{index}]", "type is required"))
    return PlanValidation(not issues, tuple(issues))


def parse_plan(raw: str | bytes) -> tuple[dict[str, Any] | None, PlanValidation]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, PlanValidation(False, (_issue("INVALID_JSON", "$", str(exc)),))
    if not isinstance(parsed, dict):
        return None, PlanValidation(False, (_issue("PLAN_NOT_OBJECT", "$", "plan must be a JSON object"),))
    result = validate_plan(parsed)
    return parsed, result


def validate_json_artifact(content: str, schema: Mapping[str, Any] | None = None) -> ArtifactValidation:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        return ArtifactValidation("fail", "json", (str(exc),))
    if schema is None:
        return ArtifactValidation("pass", "json", details={"type": type(value).__name__})
    expected_type = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str, "number": (int, float), "boolean": bool, "null": type(None)}
    if expected_type in type_map and not isinstance(value, type_map[expected_type]):
        return ArtifactValidation("fail", "json", (f"expected type {expected_type}",))
    required = schema.get("required", [])
    if isinstance(required, list) and isinstance(value, dict):
        missing = [key for key in required if key not in value]
        if missing:
            return ArtifactValidation("fail", "json", (f"missing required keys: {', '.join(missing)}",))
    return ArtifactValidation("pass", "json", details={"type": type(value).__name__})


def validate_markdown_artifact(content: str, required_headings: list[str] | None = None) -> ArtifactValidation:
    headings = re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", content)
    required = required_headings or []
    missing = [heading for heading in required if heading.casefold() not in {item.casefold() for item in headings}]
    if missing:
        return ArtifactValidation("fail", "markdown", tuple(f"missing heading: {heading}" for heading in missing))
    return ArtifactValidation("pass", "markdown", details={"headings": headings})


def validate_path_scope(paths: list[str], allowed_paths: list[str]) -> ArtifactValidation:
    allowed = {PurePosixPath(path.replace("\\", "/")).as_posix().casefold() for path in allowed_paths}
    discovered = {PurePosixPath(path.replace("\\", "/")).as_posix().casefold() for path in paths}
    violations = sorted(discovered - allowed)
    if violations:
        return ArtifactValidation("fail", "path_scope", tuple(f"path not allowed: {path}" for path in violations))
    return ArtifactValidation("pass", "path_scope", details={"paths": sorted(discovered)})


def validate_python_artifact(content: str, compile_check: bool = True) -> ArtifactValidation:
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return ArtifactValidation("fail", "python_ast", (str(exc),))
    if compile_check:
        try:
            compile(tree, "<artifact>", "exec")
        except (SyntaxError, ValueError) as exc:
            return ArtifactValidation("fail", "python_compile", (str(exc),))
    imports = [node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import) and node.names]
    imports.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    return ArtifactValidation("pass", "python_ast", details={"imports": sorted(set(imports))})


Validator = Callable[..., ArtifactValidation]
VALIDATORS: dict[str, Validator] = {
    "json": validate_json_artifact,
    "markdown": validate_markdown_artifact,
    "path_scope": validate_path_scope,
    "python": validate_python_artifact,
}


def validate_artifact(kind: str, content: Any, **options: Any) -> ArtifactValidation:
    validator = VALIDATORS.get(kind)
    if validator is None:
        return ArtifactValidation("unsupported", kind, (f"no deterministic validator for {kind}",))
    if kind == "path_scope":
        return validator(content, options.get("allowed_paths", []))
    return validator(content, **options)


def choose_retry(error_codes: list[str], attempt: int, max_attempts: int) -> RetryDecision:
    if attempt >= max_attempts:
        return RetryDecision("stop", attempt, max_attempts, "retry budget exhausted", "return_failure")
    structural = {"OBJECTIVE_REQUIRED", "CONSTRAINTS_TYPE", "SCHEMA_VERSION", "INVALID_JSON", "PLAN_NOT_OBJECT"}
    if any(code in structural for code in error_codes):
        return RetryDecision("level_3", attempt, max_attempts, "plan structure is invalid", "regenerate_plan")
    if any(code.startswith("ARTIFACT_") or code in {"INVALID_JSON", "PYTHON_SYNTAX", "PATH_SCOPE"} for code in error_codes):
        return RetryDecision("level_1", attempt, max_attempts, "localized artifact validation failed", "repair_section")
    return RetryDecision("level_2", attempt, max_attempts, "current artifact needs regeneration", "regenerate_artifact")


class RetryTelemetry:
    def __init__(self) -> None:
        self.total = 0
        self.repairs = 0
        self.plan_retries = 0
        self.artifact_retries = 0

    def record(self, decision: RetryDecision, success: bool) -> None:
        if decision.level == "stop":
            return
        self.total += 1
        self.repairs += int(success)
        self.plan_retries += int(decision.level == "level_3")
        self.artifact_retries += int(decision.level in {"level_1", "level_2"})

    def metrics(self) -> dict[str, float | int]:
        return {
            "retry_rate": self.total,
            "repair_success_rate": round(self.repairs / self.total, 4) if self.total else 0.0,
            "plan_retry_rate": self.plan_retries,
            "artifact_retry_rate": self.artifact_retries,
        }


class ExecutionProtocol:
    """Small facade that keeps plan, artifact, and retry state together."""

    def __init__(self, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self.attempt = 0
        self.telemetry = RetryTelemetry()

    def validate_plan(self, plan: Mapping[str, Any]) -> PlanValidation:
        return validate_plan(plan)

    def validate_artifact(self, kind: str, content: Any, **options: Any) -> ArtifactValidation:
        return validate_artifact(kind, content, **options)

    def next_retry(self, error_codes: list[str], success: bool = False) -> RetryDecision:
        decision = choose_retry(error_codes, self.attempt, self.max_attempts)
        self.telemetry.record(decision, success)
        if decision.level != "stop":
            self.attempt += 1
        return decision

    def metrics(self) -> dict[str, float | int]:
        return self.telemetry.metrics()
