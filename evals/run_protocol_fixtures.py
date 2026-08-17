"""Run deterministic V2 protocol state-machine fixtures and persist evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from protocol import choose_retry, validate_artifact, validate_plan


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "experiments" / "protocol-fixtures" / "results.json"
PROTOCOL = "deterministic-v2-fixtures-v1"


def valid_plan() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "objective": "Produce a validated artifact",
        "requirements": [{
            "id": "deliverable",
            "statement": "Produce one JSON artifact",
            "acceptance_criteria": ["response.json parses as JSON"],
        }],
        "hard_constraints": [{
            "id": "format",
            "type": "hard",
            "statement": "Return a JSON object",
            "strategy": "Serialize one JSON object",
            "required_gate": False,
            "failure_action": "",
        }],
        "soft_preferences": [],
        "risk_points": [],
        "artifacts": [{"path": "response.json", "kind": "json"}],
        "validation_profile": {"validators": [{"type": "json_schema"}]},
    }


def fixtures() -> list[dict[str, Any]]:
    invalid_schema = valid_plan()
    invalid_schema["schema_version"] = "0"
    unrequested_gate = valid_plan()
    unrequested_gate["hard_constraints"][0]["strategy"] = "Add a scanner that rejects the build on JSON violations"
    return [
        {
            "id": "clean-pass",
            "plans": [valid_plan()],
            "kind": "json",
            "artifacts": ['{"ok": true}'],
            "options": {"schema": {"type": "object", "required": ["ok"]}},
            "expected_termination": "success",
        },
        {
            "id": "plan-regeneration",
            "plans": [invalid_schema, valid_plan()],
            "kind": "json",
            "artifacts": ['{"ok": true}'],
            "options": {"schema": {"type": "object"}},
            "expected_termination": "success",
        },
        {
            "id": "unrequested-gate-repair",
            "plans": [unrequested_gate, valid_plan()],
            "kind": "json",
            "artifacts": ['{"ok": true}'],
            "options": {"schema": {"type": "object"}},
            "expected_termination": "success",
        },
        {
            "id": "localized-artifact-repair",
            "plans": [valid_plan()],
            "kind": "json",
            "artifacts": ['{"ok":', '{"ok": true}'],
            "options": {"schema": {"type": "object", "required": ["ok"]}},
            "expected_termination": "success",
        },
        {
            "id": "unsupported-artifact",
            "plans": [valid_plan()],
            "kind": "binary",
            "artifacts": [b"data"],
            "options": {},
            "expected_termination": "unsupported",
        },
        {
            "id": "retry-exhausted",
            "plans": [valid_plan()],
            "kind": "json",
            "artifacts": ['{"ok":', '{"ok":', '{"ok":'],
            "options": {"schema": {"type": "object"}},
            "expected_termination": "artifact_validation_exhausted",
        },
    ]


def artifact_codes(kind: str, errors: tuple[str, ...]) -> list[str]:
    if kind == "json":
        return ["ARTIFACT_INVALID_JSON"] if errors else []
    if kind == "python":
        return ["PYTHON_SYNTAX"] if errors else []
    return [f"ARTIFACT_{kind.upper()}"] if errors else []


def run_fixture(fixture: dict[str, Any], max_attempts: int = 2) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    plan_valid = False
    for index, plan in enumerate(fixture["plans"]):
        validation = validate_plan(plan)
        event = {"phase": "plan", "attempt": index + 1, "validation": validation.to_dict()}
        events.append(event)
        if validation.valid:
            plan_valid = True
            break
        codes = [issue.code for issue in validation.issues]
        decision = choose_retry(codes, index, max_attempts)
        event["decision"] = decision.to_dict()
        if decision.level == "stop":
            break
    if not plan_valid:
        termination = "plan_validation_exhausted"
    else:
        termination = "artifact_validation_exhausted"
        for index, content in enumerate(fixture["artifacts"]):
            validation = validate_artifact(fixture["kind"], content, **fixture.get("options", {}))
            event = {"phase": "artifact", "attempt": index + 1, "validation": validation.to_dict()}
            events.append(event)
            if validation.status == "pass":
                termination = "success"
                break
            if validation.status == "unsupported":
                termination = "unsupported"
                break
            decision = choose_retry(artifact_codes(fixture["kind"], validation.errors), index, max_attempts)
            event["decision"] = decision.to_dict()
            if decision.level == "stop":
                break
    retry_events = [event for event in events if event.get("decision", {}).get("level") != "stop" and event.get("decision")]
    observed_levels = [event["decision"]["level"] for event in events if event.get("decision")]
    expected = fixture["expected_termination"]
    return {
        "case_id": fixture["id"],
        "termination_reason": termination,
        "expected_termination": expected,
        "conformance_pass": termination == expected,
        "retry_count": len(retry_events),
        "plan_retry_count": sum(event["phase"] == "plan" for event in retry_events),
        "artifact_retry_count": sum(event["phase"] == "artifact" for event in retry_events),
        "retry_levels": observed_levels,
        "events": events,
    }


def run_all(max_attempts: int = 2) -> dict[str, Any]:
    results = [run_fixture(fixture, max_attempts) for fixture in fixtures()]
    retry_rows = [row for row in results if row["retry_count"]]
    return {
        "protocol": PROTOCOL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "results": results,
        "summary": {
            "passed": sum(row["conformance_pass"] for row in results),
            "failed": sum(not row["conformance_pass"] for row in results),
            "retry_rate": round(len(retry_rows) / len(results), 4) if results else 0.0,
            "average_retries": round(sum(row["retry_count"] for row in results) / len(results), 4) if results else 0.0,
            "plan_retry_rate": round(sum(row["plan_retry_count"] > 0 for row in results) / len(results), 4) if results else 0.0,
            "artifact_retry_rate": round(sum(row["artifact_retry_count"] > 0 for row in results) / len(results), 4) if results else 0.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic execution protocol fixtures.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    payload = run_all(args.max_attempts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0 if not payload["summary"]["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
