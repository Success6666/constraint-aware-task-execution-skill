from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_skill_uses_standard_discovery_layout(self) -> None:
        self.assertTrue((ROOT / "skills" / "constraint-aware-task-execution" / "SKILL.md").is_file())
        self.assertFalse((ROOT / "constraint-aware-task-execution").exists())

    def test_cross_agent_install_verifier_exists(self) -> None:
        verifier = (ROOT / "scripts" / "verify-install.py").read_text(encoding="utf-8")
        for agent in ("codex", "claude-code", "opencode"):
            self.assertIn(f'"{agent}"', verifier)

    def test_evaluation_suite_has_balanced_languages_and_categories(self) -> None:
        cases = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 30)
        self.assertEqual(Counter(case["language"] for case in cases), {"en": 15, "zh": 15})
        self.assertEqual(
            Counter(case["category"] for case in cases),
            {
                "hard_constraint": 12,
                "soft_preference": 6,
                "safety_or_explicit_enforcement": 6,
                "output_or_architecture_constraint": 6,
            },
        )


if __name__ == "__main__":
    unittest.main()
