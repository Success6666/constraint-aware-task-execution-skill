from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "constraint-exec"


class SkillStructureTests(unittest.TestCase):
    def test_skill_is_compact_and_has_no_template_markers(self) -> None:
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("TODO", content)
        self.assertLessEqual(len(content.splitlines()), 45)
        self.assertLessEqual(len(content), 3000)

    def test_skill_contains_article_core_rules(self) -> None:
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8").casefold()
        for phrase in (
            "primary objective", "hard constraints", "soft preferences", "failure gate",
            "simplest reasonable implementation", "over-optimization",
            "do not name the forbidden option again",
        ):
            self.assertIn(phrase, content)

        self.assertIn("must not add model calls", content)
        self.assertIn("every positive requirement explicitly and observably", content)
        self.assertIn("keep the answer concise and complete", content)

    def test_openai_metadata_invokes_exact_skill(self) -> None:
        content = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$constraint-exec", content)


if __name__ == "__main__":
    unittest.main()
