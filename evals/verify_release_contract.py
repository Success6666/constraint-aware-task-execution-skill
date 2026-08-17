"""Verify frozen release inputs, schemas, Skill metadata, and source secrets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from jsonschema.validators import validator_for

from redaction import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
DEFAULT_MANIFEST = EVALS / "benchmark-manifest.json"
DEFAULT_RELEASE = EVALS / "release-experiment.json"
SCHEMA_ROOT = EVALS / "schemas"
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}
IGNORED_SCAN_PREFIXES = (
    "evals/experiments/",
    "evals/results/raw/",
    "evals/results/traces/",
)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(status_errors: list[str], **details: Any) -> dict[str, Any]:
    return {"status": "pass" if not status_errors else "fail", "errors": status_errors, **details}


def verify_datasets(manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    observed: list[dict[str, Any]] = []
    for dataset in manifest.get("datasets", []):
        path = EVALS / str(dataset.get("path", ""))
        if not path.is_file():
            errors.append(f"DATASET_MISSING:{dataset.get('id')}:{path}")
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"DATASET_JSON:{dataset.get('id')}:{exc}")
            continue
        count = len(value) if isinstance(value, list) else -1
        if isinstance(value, list):
            ids = [item.get("id") for item in value if isinstance(item, dict)]
            if len(ids) != count or any(not isinstance(case_id, str) or not case_id for case_id in ids):
                errors.append(f"DATASET_CASE_ID:{dataset.get('id')}")
            elif len(set(ids)) != len(ids):
                errors.append(f"DATASET_DUPLICATE_CASE_ID:{dataset.get('id')}")
            required_fields = {
                "answer": {
                    "id", "language", "category", "prompt", "constraint_terms",
                    "objective_markers", "soft_preference",
                },
                "classification": {
                    "id", "language", "category", "text", "constraint_terms",
                    "expect_gate",
                },
                "artifact": {"id", "prompt", "allowed_paths", "validators"},
            }.get(str(dataset.get("kind")), set())
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    errors.append(f"DATASET_CASE_OBJECT:{dataset.get('id')}:{index}")
                    continue
                missing_fields = sorted(required_fields - set(item))
                if missing_fields:
                    errors.append(
                        f"DATASET_CASE_FIELDS:{dataset.get('id')}:{item.get('id', index)}:{missing_fields}"
                    )
        if count != dataset.get("declared_cases"):
            errors.append(f"DATASET_COUNT:{dataset.get('id')}:{count}")
        if digest != dataset.get("sha256"):
            errors.append(f"DATASET_DIGEST:{dataset.get('id')}:{digest}")
        observed.append({"id": dataset.get("id"), "cases": count, "sha256": digest})
    return _check(errors, datasets=observed)


def verify_release_coverage(
    manifest: dict[str, Any], release: dict[str, Any]
) -> dict[str, Any]:
    errors: list[str] = []
    datasets = {
        str(item.get("path")): str(item.get("id"))
        for item in manifest.get("datasets", [])
    }
    observed: dict[str, Any] = {}
    required_matrices = manifest.get("required_matrices", {})
    for group, requirement in required_matrices.items():
        source = release.get("runtimes", []) if group == "runtime" else release.get("matrices", [])
        entries = [item for item in source if item.get("benchmark_matrix") == group]
        models = {(item.get("executor"), item.get("model")) for item in entries}
        if len(models) < int(requirement.get("minimum_models", 1)):
            errors.append(f"RELEASE_MODELS:{group}:{len(models)}")
        required_datasets = set(requirement.get("datasets", []))
        entry_details: list[dict[str, Any]] = []
        for item in entries:
            case_files = (
                [item.get("case_file")]
                if group == "runtime"
                else item.get("case_files", [])
            )
            actual_datasets = {datasets.get(str(path)) for path in case_files}
            if None in actual_datasets:
                errors.append(f"RELEASE_DATASET_UNKNOWN:{item.get('id')}:{case_files}")
                actual_datasets.discard(None)
            if actual_datasets != required_datasets:
                errors.append(
                    f"RELEASE_DATASETS:{item.get('id')}:{sorted(actual_datasets)}"
                )
            case_ids: set[str] = set()
            for relative in case_files:
                path = EVALS / str(relative)
                if not path.is_file():
                    continue
                value = _json(path)
                if not isinstance(value, list):
                    continue
                for case in value:
                    case_id = case.get("id") if isinstance(case, dict) else None
                    if case_id in case_ids:
                        errors.append(f"RELEASE_DUPLICATE_CASE:{item.get('id')}:{case_id}")
                    if isinstance(case_id, str):
                        case_ids.add(case_id)
            dimension = "modes" if group == "runtime" else "variants"
            required_values = set(requirement.get(dimension, []))
            actual_values = set(item.get(dimension, []))
            if not required_values.issubset(actual_values):
                errors.append(
                    f"RELEASE_{dimension.upper()}:{item.get('id')}:{sorted(actual_values)}"
                )
            if int(item.get("repeats", 0)) < int(requirement.get("minimum_repeats", 1)):
                errors.append(f"RELEASE_REPEATS:{item.get('id')}:{item.get('repeats')}")
            entry_details.append({
                "id": item.get("id"),
                "executor": item.get("executor"),
                "model": item.get("model"),
                "datasets": sorted(actual_datasets),
                "cases": len(case_ids),
                dimension: sorted(actual_values),
                "repeats": item.get("repeats"),
            })
        observed[group] = {
            "entries": entry_details,
            "models": len(models),
        }

    semantic = manifest.get("semantic_review", {})
    review = release.get("review", {})
    if release.get("release_candidate") != manifest.get("repository_version"):
        errors.append("RELEASE_VERSION_MISMATCH")
    if bool(review.get("required_for_final_release")) != bool(
        semantic.get("required_for_final_release")
    ):
        errors.append("RELEASE_REVIEW_REQUIRED")
    if set(review.get("candidate_variants", [])) != set(
        semantic.get("candidate_variants", [])
    ):
        errors.append("RELEASE_REVIEW_CANDIDATES")
    if int(review.get("minimum_reviewers_per_pair", 0)) < int(
        semantic.get("minimum_reviewers_per_pair", 0)
    ):
        errors.append("RELEASE_REVIEWER_COUNT")
    if float(review.get("maximum_dimension_delta_spread", 1.0)) > float(
        semantic.get("maximum_dimension_delta_spread", 0.0)
    ):
        errors.append("RELEASE_REVIEW_DISAGREEMENT_THRESHOLD")
    return _check(errors, matrices=observed)


def _validate(instance: Any, schema_path: Path, label: str) -> list[str]:
    schema = _json(schema_path)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return [
        f"SCHEMA_INSTANCE:{label}:{'/'.join(str(item) for item in error.absolute_path) or '$'}:{error.message}"
        for error in sorted(validator_class(schema).iter_errors(instance), key=lambda item: list(item.absolute_path))
    ]


def verify_schemas(manifest: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    checked: list[str] = []
    for path in sorted(SCHEMA_ROOT.glob("*.schema.json")):
        try:
            schema = _json(path)
            validator_for(schema).check_schema(schema)
            checked.append(path.name)
        except Exception as exc:  # jsonschema exposes several schema-specific exceptions.
            errors.append(f"SCHEMA_INVALID:{path.name}:{exc}")
    for instance, schema_name, label in (
        (manifest, "benchmark-manifest.schema.json", "benchmark-manifest"),
        (release, "release-experiment.schema.json", "release-experiment"),
    ):
        try:
            errors.extend(_validate(instance, SCHEMA_ROOT / schema_name, label))
        except Exception as exc:
            errors.append(f"SCHEMA_VALIDATION_ERROR:{label}:{exc}")
    return _check(errors, schemas=checked)


def verify_skill() -> dict[str, Any]:
    errors: list[str] = []
    skill_root = ROOT / "skills" / "constraint-exec"
    skill = skill_root / "SKILL.md"
    metadata = skill_root / "agents" / "openai.yaml"
    if not skill.is_file():
        errors.append("SKILL_MISSING:skills/constraint-exec/SKILL.md")
    else:
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\nname: constraint-exec\n"):
            errors.append("SKILL_FRONTMATTER_NAME")
        if "$constraint-exec" in text:
            errors.append("SKILL_SELF_INVOCATION")
    if not metadata.is_file() or "$constraint-exec" not in metadata.read_text(encoding="utf-8"):
        errors.append("SKILL_METADATA_INVOCATION")
    return _check(errors, path=str(skill.relative_to(ROOT)))


def _source_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git ls-files failed")
    paths = []
    for relative in completed.stdout.splitlines():
        normalized = relative.replace("\\", "/")
        if normalized.startswith(IGNORED_SCAN_PREFIXES):
            continue
        paths.append(ROOT / relative)
    return paths


def verify_secrets() -> dict[str, Any]:
    errors: list[str] = []
    scanned = 0
    forbidden_names = {"auth.json", ".env"}
    for path in _source_files():
        if not path.is_file():
            continue
        if path.name in forbidden_names or path.suffix.casefold() in {".pem", ".key"}:
            errors.append(f"SECRET_FILE:{path.relative_to(ROOT).as_posix()}")
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        scanned += 1
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"SECRET_PATTERN:{name}:{path.relative_to(ROOT).as_posix()}")
    return _check(errors, scanned_files=scanned, unredacted_secret_count=len(errors))


def build_report(manifest_path: Path, release_path: Path) -> dict[str, Any]:
    manifest = _json(manifest_path)
    release = _json(release_path)
    checks = {
        "dataset_integrity": verify_datasets(manifest),
        "release_coverage": verify_release_coverage(manifest, release),
        "schema_validation": verify_schemas(manifest, release),
        "skill_validation": verify_skill(),
        "secret_scan": verify_secrets(),
    }
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if all(item["status"] == "pass" for item in checks.values()) else "fail",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deterministic release contracts.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.manifest.resolve(), args.release.resolve())
    atomic_write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
