from __future__ import annotations

from collections import Counter
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VERIFY_INSTALL_PATH = ROOT / "scripts" / "verify-install.py"
VERIFY_INSTALL_SPEC = importlib.util.spec_from_file_location("verify_install", VERIFY_INSTALL_PATH)
assert VERIFY_INSTALL_SPEC and VERIFY_INSTALL_SPEC.loader
verify_install = importlib.util.module_from_spec(VERIFY_INSTALL_SPEC)
VERIFY_INSTALL_SPEC.loader.exec_module(verify_install)


class DistributionTests(unittest.TestCase):
    def test_skill_uses_standard_discovery_layout(self) -> None:
        self.assertTrue((ROOT / "skills" / "constraint-aware-task-execution" / "SKILL.md").is_file())
        self.assertFalse((ROOT / "constraint-aware-task-execution").exists())

    def test_cross_agent_install_verifier_exists(self) -> None:
        verifier = (ROOT / "scripts" / "verify-install.py").read_text(encoding="utf-8")
        for agent in ("codex", "claude-code", "opencode"):
            self.assertIn(f'"{agent}"', verifier)

    def test_discovery_output_normalizes_terminal_codes(self) -> None:
        listing = (
            f"Source: /tmp/{verify_install.SKILL_NAME}-skill\r\n"
            f"│    \x1b[36m{verify_install.SKILL_NAME}\x1b[39m\r\n"
        )
        self.assertEqual(
            verify_install.discovered_skill_count(listing),
            1,
        )

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

    def test_adversarial_gate_cases_cover_both_directions_and_languages(self) -> None:
        cases = json.loads((ROOT / "evals" / "adversarial_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 6)
        self.assertEqual({case["expect_gate"] for case in cases}, {True, False})
        self.assertEqual({case["language"] for case in cases}, {"en", "zh"})


if __name__ == "__main__":
    unittest.main()
