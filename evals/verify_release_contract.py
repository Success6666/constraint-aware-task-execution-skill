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
        if count != dataset.get("declared_cases"):
            errors.append(f"DATASET_COUNT:{dataset.get('id')}:{count}")
        if digest != dataset.get("sha256"):
            errors.append(f"DATASET_DIGEST:{dataset.get('id')}:{digest}")
        observed.append({"id": dataset.get("id"), "cases": count, "sha256": digest})
    return _check(errors, datasets=observed)


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
