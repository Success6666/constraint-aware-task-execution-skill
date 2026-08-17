from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

from .base import ValidationContext, ValidatorResult


def _result(kind: str, validator_id: str, errors: list[str] | tuple[str, ...] = (), **details: Any) -> ValidatorResult:
    return ValidatorResult("pass" if not errors else "fail", kind, validator_id, tuple(errors), details)


def safe_path(context: ValidationContext, relative: str) -> Path:
    candidate = (context.workspace / relative).resolve()
    root = context.workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"PATH_ESCAPE:{relative}")
    return candidate


def project_paths(context: ValidationContext) -> set[str]:
    ignored = {"AGENTS.md"}
    return {
        path.relative_to(context.workspace).as_posix()
        for path in context.workspace.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".codex" not in path.parts
        and "__pycache__" not in path.parts
        and path.name not in ignored
    }


def path_scope(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    allowed = {path.replace("\\", "/") for path in (spec.get("allowed_paths") or context.allowed_paths)}
    observed = (
        {path.replace("\\", "/") for path in context.changed_paths}
        if context.changed_paths is not None
        else project_paths(context)
    )
    unexpected = sorted(observed - allowed)
    return _result(
        "path_scope",
        validator_id,
        [f"PATH_SCOPE:{path}" for path in unexpected],
        paths=sorted(observed),
    )


def files_exist(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    errors: list[str] = []
    for relative in spec.get("paths", []):
        try:
            exists = safe_path(context, str(relative)).is_file()
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not exists:
            errors.append(f"FILE_MISSING:{relative}")
    return _result("files_exist", validator_id, errors)


def _load_json(relative: str, context: ValidationContext) -> tuple[Any, str | None]:
    try:
        return json.loads(safe_path(context, relative).read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"JSON_INVALID:{relative}:{exc}"


def json_exact(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    relative = str(spec.get("path", ""))
    value, error = _load_json(relative, context)
    errors = [error] if error else ([] if value == spec.get("value") else [f"JSON_VALUE:{relative}"])
    return _result("json_exact", validator_id, errors)


_SCHEMA_KEYWORDS = {
    "$id",
    "$schema",
    "additionalProperties",
    "const",
    "default",
    "description",
    "enum",
    "examples",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
}
_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _schema_support_errors(schema: Mapping[str, Any], path: str = "$") -> list[str]:
    errors = [f"JSON_SCHEMA_KEYWORD_UNSUPPORTED:{path}:{key}" for key in schema if key not in _SCHEMA_KEYWORDS]
    expected = schema.get("type")
    if expected is not None and (not isinstance(expected, str) or expected not in _SCHEMA_TYPES):
        errors.append(f"JSON_SCHEMA_TYPE_UNSUPPORTED:{path}:{expected}")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        errors.append(f"JSON_SCHEMA_DEFINITION:{path}.required")
    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        errors.append(f"JSON_SCHEMA_DEFINITION:{path}.enum")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        errors.append(f"JSON_SCHEMA_KEYWORD_UNSUPPORTED:{path}:additionalProperties")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(keyword)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append(f"JSON_SCHEMA_DEFINITION:{path}.{keyword}")
    for keyword in ("minimum", "maximum"):
        value = schema.get(keyword)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            errors.append(f"JSON_SCHEMA_DEFINITION:{path}.{keyword}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        errors.append(f"JSON_SCHEMA_DEFINITION:{path}.properties")
    else:
        for key, child in properties.items():
            if not isinstance(child, Mapping):
                errors.append(f"JSON_SCHEMA_DEFINITION:{path}.properties.{key}")
            else:
                errors.extend(_schema_support_errors(child, f"{path}.{key}"))
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            errors.append(f"JSON_SCHEMA_DEFINITION:{path}.items")
        else:
            errors.extend(_schema_support_errors(items, f"{path}[]"))
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            errors.append(f"JSON_SCHEMA_DEFINITION:{path}.pattern")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"JSON_SCHEMA_PATTERN_INVALID:{path}:{exc}")
    return errors


def _schema_errors(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool, "null": type(None)}
    if expected in type_map and (not isinstance(value, type_map[expected]) or expected in {"number", "integer"} and isinstance(value, bool)):
        return [f"JSON_SCHEMA_TYPE:{path}:{expected}"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"JSON_SCHEMA_REQUIRED:{path}.{key}")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                errors.extend(_schema_errors(value[key], child, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            errors.extend(f"JSON_SCHEMA_ADDITIONAL:{path}.{key}" for key in value if key not in properties)
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, child in enumerate(value):
            errors.extend(_schema_errors(child, schema["items"], f"{path}[{index}]"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"JSON_SCHEMA_ENUM:{path}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"JSON_SCHEMA_CONST:{path}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"JSON_SCHEMA_MIN_LENGTH:{path}")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"JSON_SCHEMA_MAX_LENGTH:{path}")
        if isinstance(schema.get("pattern"), str) and re.search(schema["pattern"], value) is None:
            errors.append(f"JSON_SCHEMA_PATTERN:{path}")
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"JSON_SCHEMA_MIN_ITEMS:{path}")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"JSON_SCHEMA_MAX_ITEMS:{path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"JSON_SCHEMA_MINIMUM:{path}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"JSON_SCHEMA_MAXIMUM:{path}")
    return errors


def json_schema(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    relative = str(spec.get("path", ""))
    value, error = _load_json(relative, context)
    if error:
        return _result("json_schema", validator_id, [error])
    schema = spec.get("schema", {})
    if not isinstance(schema, Mapping):
        return ValidatorResult("unsupported", "json_schema", validator_id, ("JSON_SCHEMA_UNSUPPORTED",))
    support_errors = _schema_support_errors(schema)
    if support_errors:
        return ValidatorResult("unsupported", "json_schema", validator_id, tuple(support_errors))
    return _result("json_schema", validator_id, _schema_errors(value, schema))


def markdown_headings(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    relative = str(spec.get("path", ""))
    try:
        content = safe_path(context, relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return _result("markdown_headings", validator_id, [f"FILE_READ:{relative}:{exc}"])
    headings = {item.casefold() for item in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", content)}
    errors = [f"MARKDOWN_HEADING:{heading}" for heading in spec.get("headings", []) if str(heading).casefold() not in headings]
    return _result("markdown_headings", validator_id, errors, headings=sorted(headings))


def python_compile(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    errors: list[str] = []
    imports: dict[str, list[str]] = {}
    for relative in spec.get("paths", []):
        try:
            source = safe_path(context, str(relative)).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(relative))
            compile(tree, str(relative), "exec")
            found = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            found.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
            imports[str(relative)] = sorted(set(found))
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            errors.append(f"PYTHON_COMPILE:{relative}:{exc}")
    return _result("python_compile", validator_id, errors, imports=imports)


def forbidden_imports(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    forbidden = {str(name).split(".")[0] for name in spec.get("imports", [])}
    errors: list[str] = []
    for relative in spec.get("paths", []):
        try:
            tree = ast.parse(safe_path(context, str(relative)).read_text(encoding="utf-8"), filename=str(relative))
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            errors.append(f"PYTHON_AST:{relative}:{exc}")
            continue
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update((node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        errors.extend(f"FORBIDDEN_IMPORT:{relative}:{name}" for name in sorted(imports & forbidden))
    return _result("forbidden_imports", validator_id, errors)


def forbidden_pattern(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    errors: list[str] = []
    try:
        patterns = [re.compile(str(pattern)) for pattern in spec.get("patterns", [])]
    except re.error as exc:
        return ValidatorResult("unsupported", "forbidden_pattern", validator_id, (f"PATTERN_INVALID:{exc}",))
    for relative in spec.get("paths", []):
        try:
            content = safe_path(context, str(relative)).read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"FILE_READ:{relative}:{exc}")
            continue
        errors.extend(f"FORBIDDEN_PATTERN:{relative}:{pattern.pattern}" for pattern in patterns if pattern.search(content))
    return _result("forbidden_pattern", validator_id, errors)


def _command_allowed(command: list[str], context: ValidationContext) -> bool:
    lowered = tuple(item.casefold() for item in command)
    return any(lowered[:len(prefix)] == tuple(item.casefold() for item in prefix) for prefix in context.allowed_commands)


def command(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    requested = spec.get("command", [])
    if not isinstance(requested, list) or not requested or not all(isinstance(item, str) and item for item in requested):
        return ValidatorResult("unsupported", "command", validator_id, ("COMMAND_INVALID",))
    if not _command_allowed(requested, context):
        return ValidatorResult("unsupported", "command", validator_id, ("COMMAND_NOT_ALLOWLISTED",))
    if shutil.which(requested[0]) is None:
        return ValidatorResult("unsupported", "command", validator_id, (f"COMMAND_UNAVAILABLE:{requested[0]}",))
    try:
        completed = subprocess.run(
            requested,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(int(spec.get("timeout", context.timeout_seconds)), context.timeout_seconds),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _result("command", validator_id, ["COMMAND_TIMEOUT"])
    output = (completed.stdout + completed.stderr)[-context.max_output_chars:]
    return _result("command", validator_id, [] if completed.returncode == 0 else [f"COMMAND_FAILED:{completed.returncode}"], returncode=completed.returncode, output=output)


def javascript_syntax(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    results: list[str] = []
    for relative in spec.get("paths", []):
        result = command({"command": ["node", "--check", str(safe_path(context, str(relative)))]}, context, f"{validator_id}:{relative}")
        if result.status == "unsupported":
            return ValidatorResult("unsupported", "javascript_syntax", validator_id, result.errors)
        results.extend(f"JAVASCRIPT_SYNTAX:{relative}:{error}" for error in result.errors)
    return _result("javascript_syntax", validator_id, results)


def typescript_check(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    if shutil.which("tsc") is None:
        return ValidatorResult("unsupported", "typescript_check", validator_id, ("COMMAND_UNAVAILABLE:tsc",))
    try:
        paths = [str(safe_path(context, str(relative))) for relative in spec.get("paths", [])]
    except ValueError as exc:
        return _result("typescript_check", validator_id, [str(exc)])
    result = command({"command": ["tsc", "--noEmit", *paths]}, context, validator_id)
    return ValidatorResult(result.status, "typescript_check", validator_id, result.errors, result.details)


def yaml_parse(spec: Mapping[str, Any], context: ValidationContext, validator_id: str) -> ValidatorResult:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return ValidatorResult("unsupported", "yaml_parse", validator_id, ("DEPENDENCY_UNAVAILABLE:PyYAML",))
    errors: list[str] = []
    for relative in spec.get("paths", []):
        try:
            yaml.safe_load(safe_path(context, str(relative)).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"YAML_INVALID:{relative}:{exc}")
    return _result("yaml_parse", validator_id, errors)
