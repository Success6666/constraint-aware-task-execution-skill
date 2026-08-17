from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from run_runtime_fixtures import run_all  # noqa: E402


class RuntimeFixtureTests(unittest.TestCase):
    def test_positive_and_negative_workspaces_are_classified(self) -> None:
        payload = run_all()
        self.assertEqual(payload["cases"], 12)
        self.assertEqual(payload["summary"]["passed"], 12)
        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertTrue(all(row["conformance_pass"] for row in payload["results"]))


if __name__ == "__main__":
    unittest.main()
