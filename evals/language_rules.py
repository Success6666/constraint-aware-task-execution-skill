"""Conservative multilingual rules for explicit constraint metadata.

These helpers classify declared wording. They are not a semantic quality judge;
ambiguous text remains unknown and must not be promoted to a hard constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


LANGUAGE_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "hard": (r"\bmust\b", r"\brequired\b", r"\bonly\b", r"\bdo not\b", r"\bnever\b"),
        "soft": (r"\bprefer\b", r"\bideally\b", r"\bif practical\b", r"\bwhen possible\b"),
        "enforcement": (r"\breject\b", r"\bblock\b", r"\bfail(?:ure)?\b", r"\bquarantine\b"),
        "negative": (r"\bdo not\b", r"\bmust not\b", r"\bnever\b", r"\bwithout\b", r"\bavoid\b"),
    },
    "zh": {
        "hard": (r"必须", r"不得", r"只能", r"仅限", r"不要", r"禁止"),
        "soft": (r"优先", r"尽量", r"最好", r"如可行", r"条件允许"),
        "enforcement": (r"拒绝", r"阻止", r"失败", r"拦截", r"隔离"),
        "negative": (r"不得", r"不要", r"禁止", r"不能", r"避免", r"无需"),
    },
    "es": {
        "hard": (r"\bdebe\b", r"\bobligatorio\b", r"\bsolo\b", r"\bnunca\b"),
        "soft": (r"\bpreferir\b", r"\bpreferiblemente\b", r"\bsi es posible\b"),
        "enforcement": (r"\brechazar\b", r"\bbloquear\b", r"\bfallar\b", r"\bcuarentena\b"),
        "negative": (r"\bno\b", r"\bnunca\b", r"\bsin\b", r"\bevitar\b"),
    },
    "ja": {
        "hard": (r"必須", r"のみ", r"してはならない", r"禁止", r"絶対に"),
        "soft": (r"推奨", r"可能であれば", r"できれば", r"望ましい"),
        "enforcement": (r"拒否", r"ブロック", r"失敗", r"隔離"),
        "negative": (r"しない", r"してはならない", r"禁止", r"避ける", r"不要"),
    },
}

QUOTED_OR_CODE = re.compile(
    r"```.*?```|`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|“[^”\n]+”|‘[^’\n]+’",
    re.DOTALL,
)


@dataclass(frozen=True)
class LanguageClassification:
    language: str
    kind: str
    polarity: str
    confidence: str


def strip_quoted_examples(text: str) -> str:
    return QUOTED_OR_CODE.sub(" ", text)


def _matches(text: str, language: str, group: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in LANGUAGE_RULES[language][group])


def classify_statement(text: str, language: str | None = None) -> LanguageClassification:
    normalized = strip_quoted_examples(text).strip()
    languages = (language,) if language in LANGUAGE_RULES else tuple(LANGUAGE_RULES)
    for candidate in languages:
        if _matches(normalized, candidate, "enforcement") and _matches(normalized, candidate, "hard"):
            polarity = "forbid" if _matches(normalized, candidate, "negative") else "require"
            return LanguageClassification(candidate, "enforcement", polarity, "explicit")
    for candidate in languages:
        if _matches(normalized, candidate, "hard"):
            polarity = "forbid" if _matches(normalized, candidate, "negative") else "require"
            return LanguageClassification(candidate, "hard", polarity, "explicit")
    for candidate in languages:
        if _matches(normalized, candidate, "soft"):
            polarity = "avoid" if _matches(normalized, candidate, "negative") else "prefer"
            return LanguageClassification(candidate, "soft", polarity, "explicit")
    return LanguageClassification(language or "unknown", "unknown", "unknown", "ambiguous")

