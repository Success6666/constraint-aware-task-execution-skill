from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "constraint-exec"


class SkillStructureTests(unittest.TestCase):
    def test_skill_is_compact_and_has_no_template_markers(self) -> None:
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("TODO", content)
        self.assertLessEqual(len(content.splitlines()), 100)

    def test_skill_contains_article_core_rules(self) -> None:
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8").casefold()
        for phrase in (
            "primary objective", "hard constraints", "soft preferences", "failure gate",
            "simplest reasonable implementation", "over-optimization", "proportional",
            "do not name the forbidden option again",
        ):
            self.assertIn(phrase, content)

    def test_openai_metadata_invokes_exact_skill(self) -> None:
        content = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$constraint-exec", content)


if __name__ == "__main__":
    unittest.main()
