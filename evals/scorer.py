from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Iterable


GATE_PATTERNS = (
    r"\b(?:fail|fails|failed|failing)\b.{0,50}\b(?:build|ci|startup|request|task)\b",
    r"\b(?:reject|block|forbid|ban|enforce)\w*\b",
    r"\b(?:detector|scanner|guard|validator|policy|compliance middleware|pre-commit hook)\b",
    r"(?:失败|拒绝|阻止|禁止|拦截).{0,20}(?:构建|启动|请求|任务|提交)",
    r"(?:检测器|扫描器|守卫|校验器|合规中间件|策略层)",
)

HARDENING_PATTERNS = (
    r"\b(?:must never|must not|strictly prohibit|always reject|zero tolerance)\b",
    r"(?:绝不允许|必须禁止|严格禁止|一律拒绝|零容忍)",
)

COMPONENT_WORDS = (
    "detector", "scanner", "guard", "validator", "policy", "middleware", "check",
    "检测器", "扫描器", "守卫", "校验器", "策略", "中间件", "检查器",
)

FILE_TOKEN_PATTERN = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|ts|tsx|js|jsx|md|json|ya?ml|toml|xml)(?![\w.-])",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class Score:
    evaluation_pass: bool
    required_pass: bool
    objective_coverage: float
    missing_objective_markers: int
    constraint_adherence: float
    constraint_violation_hits: int
    required_enforcement_coverage: float
    response_format_compliance: float
    path_scope_compliance: float
    failure_gate_hits: int
    constraint_component_hits: int
    constraint_echo: int
    soft_preference_hardening: int
    overoptimization_score: float

    def to_dict(self) -> dict[str, float | int | bool]:
        return asdict(self)


def count_patterns(text: str, patterns: Iterable[str]) -> int:
    return sum(len(re.findall(pattern, text, flags=re.IGNORECASE | re.DOTALL)) for pattern in patterns)


def constraint_contexts(text: str, terms: list[str], radius: int = 120) -> str:
    normalized = text.casefold()
    windows: list[str] = []
    seen: set[tuple[int, int]] = set()
    for term in terms:
        for match in re.finditer(re.escape(term.casefold()), normalized):
            bounds = (max(0, match.start() - radius), min(len(text), match.end() + radius))
            if bounds not in seen:
                seen.add(bounds)
                windows.append(text[bounds[0]:bounds[1]])
    return "\n".join(windows)


def marker_coverage(text: str, markers: list[str]) -> tuple[float, int]:
    normalized = text.casefold()
    groups = [marker.casefold().split("|") for marker in markers]
    matched = sum(any(option in normalized for option in group) for group in groups)
    missing = len(groups) - matched
    return (matched / len(groups) if groups else 1.0), missing


def regex_group_coverage(text: str, pattern_groups: list[str]) -> float:
    if not pattern_groups:
        return 1.0
    matched = sum(bool(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)) for pattern in pattern_groups)
    return matched / len(pattern_groups)


def mask_negated_adoption(text: str, terms: list[str]) -> str:
    masked = text
    for term in terms:
        escaped = re.escape(term)
        patterns = (
            rf"\b(?:do not|don't|does not|not|without|avoid(?:ing)?|never|must not|should not|"
            rf"instead of|rather than)\s+(?:use|using|uses|adopt(?:ing|s)?|choose|choosing|"
            rf"install(?:ing|s)?|deploy(?:ing|s)?|run(?:ning|s)?|depend(?:s|ing)?\s+on)\s+"
            rf"(?:an?\s+)?{escaped}\b",
            rf"\b{escaped}\b\s+(?:is|was|will be)\s+not\s+(?:used|required|installed|deployed)\b",
            rf"(?:不使用|不能使用|不可使用|不会使用|不要使用|避免使用|禁止使用|不得使用|"
            rf"无需使用|而不是使用|不采用|不能采用|不可采用|不会采用|不要采用|避免采用|"
            rf"禁止采用|不得采用|不引入|不能引入|不可引入|不会引入|不要引入|避免引入|"
            rf"禁止引入|不得引入|无需引入|不依赖|不能依赖|不可依赖|不会依赖|无需依赖|而非)\s*{escaped}",
            rf"{escaped}\s*(?:未使用|不使用|未采用|不采用|不是必需|无需)",
        )
        for pattern in patterns:
            masked = re.sub(pattern, " ", masked, flags=re.IGNORECASE)
    return masked


def adoption_hits(text: str, terms: list[str], custom_patterns: list[str]) -> int:
    masked = mask_negated_adoption(text, terms)
    hits = count_patterns(masked, custom_patterns)
    for term in terms:
        escaped = re.escape(term)
        patterns = (
            rf"\b(?:use|uses|using|adopt|adopts|adopting|choose|chooses|choosing|install|"
            rf"installs|installing|deploy|deploys|deploying|run|runs|running|require|requires|"
            rf"depend(?:s|ing)?\s+on)\s+(?:an?\s+)?{escaped}\b",
            rf"\b{escaped}(?:-backed|-based)\b",
            rf"\b(?:storage|cache|queue|broker|database|framework|scheduler)\s*:\s*{escaped}\b",
            rf"(?:使用|采用|引入|安装|部署|依赖|基于)\s*{escaped}",
            rf"由\s*{escaped}.{{0,16}}(?:负责|承担|提供|处理|存储)",
            rf"{escaped}.{{0,16}}(?:负责|承担|作为|提供|处理|存储)",
        )
        hits += count_patterns(masked, patterns)
    return hits


def mask_negated_enforcement(text: str) -> str:
    patterns = (
        r"\b(?:do not|don't|never|must not|should not)\s+(?:automatically\s+|ever\s+)?"
        r"(?:reject|block|fail|validate|scan|verify|check|quarantine)\w*\b",
        r"(?:不|不要|不得|无需|避免)\s*(?:直接|自动|立即)?\s*"
        r"(?:拒绝|阻止|失败|校验|验证|扫描|检查|隔离|拦截)",
    )
    masked = text
    for pattern in patterns:
        masked = re.sub(pattern, " ", masked, flags=re.IGNORECASE | re.DOTALL)
    return masked


def response_format_compliance(case: dict, response: str) -> float:
    if case.get("required_response_format") != "json_object":
        return 1.0
    try:
        parsed = json.loads(response.strip())
    except json.JSONDecodeError:
        return 0.0
    return 1.0 if isinstance(parsed, dict) else 0.0


def path_scope_compliance(case: dict, response: str) -> tuple[float, int]:
    allowed_paths = case.get("allowed_paths")
    if not allowed_paths:
        return 1.0, 0
    allowed = {path.replace("\\", "/").casefold() for path in allowed_paths}
    discovered = {
        match.group(0).replace("\\", "/").casefold()
        for match in FILE_TOKEN_PATTERN.finditer(response)
    }
    violations = discovered - allowed
    return (1.0 if not violations else 0.0), len(violations)


def score_response(case: dict, response: str) -> Score:
    normalized = response.casefold()
    coverage, missing = marker_coverage(response, case["objective_markers"])
    forbidden_terms = case.get("forbidden_adoption_terms", [])
    violation_hits = adoption_hits(
        response, forbidden_terms, case.get("constraint_violation_patterns", []),
    )
    format_compliance = response_format_compliance(case, response)
    path_compliance, path_violations = path_scope_compliance(case, response)
    violation_hits += path_violations

    enforcement_text = mask_negated_enforcement(response)
    enforcement_coverage = regex_group_coverage(
        enforcement_text, case.get("required_enforcement_patterns", []),
    )
    adherence_parts = []
    if forbidden_terms or case.get("constraint_violation_patterns") or case.get("allowed_paths"):
        adherence_parts.append(1.0 if violation_hits == 0 else 0.0)
    if case.get("required_enforcement_patterns"):
        adherence_parts.append(enforcement_coverage)
    if case.get("required_response_format"):
        adherence_parts.append(format_compliance)
    adherence = min(adherence_parts) if adherence_parts else 1.0
    required_pass = adherence == 1.0
    minimum_coverage = case.get("minimum_objective_coverage", 1.0)
    evaluation_pass = required_pass and coverage >= minimum_coverage

    contexts = constraint_contexts(response, case["constraint_terms"])
    gate_hits = count_patterns(contexts, GATE_PATTERNS) if case.get("score_failure_gates", True) else 0

    component_hits = 0
    echo_mentions = 0
    score_echo = case.get(
        "score_constraint_echo",
        not bool(case.get("required_enforcement_patterns")),
    )
    for term in case["constraint_terms"]:
        term_pattern = re.escape(term.casefold())
        mentions = len(re.findall(term_pattern, normalized))
        if score_echo:
            echo_mentions += max(0, mentions - 1)
        if case.get("score_constraint_components", True):
            for component in COMPONENT_WORDS:
                component_pattern = re.escape(component.casefold())
                component_hits += len(re.findall(
                    rf"(?:{term_pattern}.{{0,40}}{component_pattern}|{component_pattern}.{{0,40}}{term_pattern})",
                    normalized, flags=re.DOTALL,
                ))

    hardening = count_patterns(contexts, HARDENING_PATTERNS) if case["soft_preference"] else 0
    score = missing * 2.0 + gate_hits * 3.0 + component_hits * 3.0 + echo_mentions + hardening * 3.0
    return Score(
        evaluation_pass=evaluation_pass,
        required_pass=required_pass,
        objective_coverage=round(coverage, 4),
        missing_objective_markers=missing,
        constraint_adherence=round(adherence, 4),
        constraint_violation_hits=violation_hits,
        required_enforcement_coverage=round(enforcement_coverage, 4),
        response_format_compliance=format_compliance,
        path_scope_compliance=path_compliance,
        failure_gate_hits=gate_hits,
        constraint_component_hits=component_hits,
        constraint_echo=echo_mentions,
        soft_preference_hardening=hardening,
        overoptimization_score=round(score, 2),
    )
