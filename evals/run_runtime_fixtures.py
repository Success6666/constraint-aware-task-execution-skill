"""Exercise runtime artifact validators against real positive and negative workspaces."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import textwrap
from typing import Any

from run_runtime import CASES_PATH, validate_runtime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "experiments" / "runtime-fixtures" / "results.json"
PROTOCOL = "runtime-validator-fixtures-v1"


PASSING_ARTIFACTS: dict[str, dict[str, str]] = {
    "json-config": {
        "config.json": '{"service_name":"catalog","port":8080,"debug":false}\n',
    },
    "parser-regression": {
        "app/parser.py": textwrap.dedent("""
            def parse_numbers(text):
                if not text or not text.strip():
                    raise ValueError("empty input")
                values = []
                for item in text.split(","):
                    if not item.strip():
                        raise ValueError("malformed item")
                    try:
                        values.append(int(item.strip()))
                    except ValueError as exc:
                        raise ValueError("malformed item") from exc
                return values
        """).lstrip(),
        "tests/test_parser.py": textwrap.dedent("""
            import sys
            from pathlib import Path
            import unittest

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from app.parser import parse_numbers

            class ParserTests(unittest.TestCase):
                def test_values(self):
                    self.assertEqual(parse_numbers("1, 2,-3"), [1, 2, -3])

                def test_invalid(self):
                    for value in ("", "1,,2", "x"):
                        with self.assertRaises(ValueError):
                            parse_numbers(value)
        """).lstrip(),
    },
    "markdown-contract": {
        "README.md": "# Checksum CLI\n\n## Overview\n\nComputes file checksums.\n\n## Usage\n\nRun `checksum FILE`.\n\n## Tests\n\nRun the test suite.\n",
    },
    "csv-report-no-pandas": {
        "report.py": textwrap.dedent("""
            import csv

            def summarize(path):
                totals = {}
                with open(path, newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        totals[row["product"]] = totals.get(row["product"], 0.0) + float(row["amount"])
                return totals
        """).lstrip(),
        "tests/test_report.py": textwrap.dedent("""
            import tempfile
            from pathlib import Path
            import sys
            import unittest

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from report import summarize

            class ReportTests(unittest.TestCase):
                def test_totals(self):
                    with tempfile.TemporaryDirectory() as temp:
                        path = Path(temp) / "data.csv"
                        path.write_text("product,amount\\na,1.5\\na,2.5\\n", encoding="utf-8")
                        self.assertEqual(summarize(path), {"a": 4.0})
        """).lstrip(),
    },
    "upload-signature-guard": {
        "upload_guard.py": textwrap.dedent("""
            def validate_upload(data):
                if data.startswith(b"MZ") or data.startswith(b"\\x7fELF"):
                    raise ValueError("executable signature")
                return True
        """).lstrip(),
        "tests/test_upload_guard.py": textwrap.dedent("""
            import unittest
            from upload_guard import validate_upload

            class UploadTests(unittest.TestCase):
                def test_signatures(self):
                    for value in (b"MZdata", b"\\x7fELFdata"):
                        with self.assertRaises(ValueError):
                            validate_upload(value)
                    self.assertTrue(validate_upload(b"ordinary"))
        """).lstrip(),
    },
    "client-no-global-singleton": {
        "src/client.py": textwrap.dedent("""
            class ApiClient:
                def __init__(self, base_url):
                    self.base_url = base_url.rstrip("/")

                def build_url(self, path):
                    return f"{self.base_url}/{path.lstrip('/')}"
        """).lstrip(),
        "tests/test_client.py": textwrap.dedent("""
            import sys
            from pathlib import Path
            import unittest

            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from client import ApiClient

            class ClientTests(unittest.TestCase):
                def test_url(self):
                    self.assertEqual(ApiClient("https://example.test/").build_url("/v1"), "https://example.test/v1")
        """).lstrip(),
    },
}


def write_artifacts(workspace: Path, artifacts: dict[str, str]) -> None:
    for relative, content in artifacts.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def break_artifact(case_id: str, workspace: Path) -> None:
    if case_id == "json-config":
        (workspace / "config.json").write_text('{"service_name":"catalog","port":8080,"debug":true}', encoding="utf-8")
    elif case_id == "parser-regression":
        (workspace / "app/parser.py").write_text("def parse_numbers(:\n", encoding="utf-8")
    elif case_id == "markdown-contract":
        (workspace / "README.md").write_text("# Checksum CLI\n\n## Overview\n", encoding="utf-8")
    elif case_id == "csv-report-no-pandas":
        (workspace / "report.py").write_text("import pandas\n", encoding="utf-8")
    elif case_id == "upload-signature-guard":
        (workspace / "upload_guard.py").write_text("def validate_upload(:\n", encoding="utf-8")
    elif case_id == "client-no-global-singleton":
        path = workspace / "src/client.py"
        path.write_text(path.read_text(encoding="utf-8") + "\nclient = ApiClient('https://example.test')\n", encoding="utf-8")
    else:
        raise ValueError(f"Unknown fixture: {case_id}")


def run_all() -> dict[str, Any]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results = []
    for case in cases:
        artifacts = PASSING_ARTIFACTS[case["id"]]
        for mode, expected_pass in (("positive", True), ("negative", False)):
            with tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                write_artifacts(workspace, artifacts)
                if mode == "negative":
                    break_artifact(case["id"], workspace)
                validations = validate_runtime(case, workspace)
                observed_pass = all(item["status"] == "pass" for item in validations)
                results.append({
                    "case_id": case["id"],
                    "mode": mode,
                    "expected_contract_pass": expected_pass,
                    "observed_contract_pass": observed_pass,
                    "conformance_pass": observed_pass == expected_pass,
                    "validations": validations,
                })
    passed = sum(row["conformance_pass"] for row in results)
    return {
        "protocol": PROTOCOL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "results": results,
        "summary": {
            "passed": passed,
            "failed": len(results) - passed,
            "retry_rate": 0.0,
            "average_retries": 0.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-workspace validator fixtures.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = run_all()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0 if not payload["summary"]["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
