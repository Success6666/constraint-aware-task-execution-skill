from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from run_protocol_fixtures import run_all  # noqa: E402


class ProtocolFixtureTests(unittest.TestCase):
    def test_all_fixture_outcomes_match_their_contracts(self) -> None:
        payload = run_all()
        self.assertEqual(payload["cases"], 6)
        self.assertEqual(payload["summary"]["passed"], 6)
        self.assertEqual(payload["summary"]["failed"], 0)
        by_id = {row["case_id"]: row for row in payload["results"]}
        self.assertIn("level_3", by_id["plan-regeneration"]["retry_levels"])
        self.assertIn("level_1", by_id["localized-artifact-repair"]["retry_levels"])
        self.assertEqual(by_id["unsupported-artifact"]["termination_reason"], "unsupported")
        self.assertEqual(by_id["retry-exhausted"]["termination_reason"], "artifact_validation_exhausted")


if __name__ == "__main__":
    unittest.main()
