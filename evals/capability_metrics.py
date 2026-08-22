"""Paired capability-retention metrics for baseline and skill variants.

The metrics in this module are deliberately evidence-bounded. Missing or
unsupported observations are excluded from denominators instead of being
coerced to zero, and broad semantic quality is never inferred from regexes.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


QUALITY_COMPONENTS = (
    "objective_coverage",
    "non_constraint_requirement_coverage",
    "declared_quality_score",
    "constraint_compliance",
    "format_compliance",
    "path_compliance",
    "artifact_contract",
)

NON_CONSTRAINT_COMPONENTS = (
    "objective_coverage",
    "non_constraint_requirement_coverage",
    "declared_quality_score",
    "format_compliance",
    "path_compliance",
    "artifact_contract",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_number(record: Mapping[str, Any], *paths: str) -> float | None:
    for path in paths:
        current: Any = record
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        number = _number(current)
        if number is not None:
            return number
    return None


def _observed_flag(record: Mapping[str, Any], *paths: str) -> bool | None:
    value = _first_number(record, *paths)
    return None if value is None else bool(value)


def _component(record: Mapping[str, Any], name: str) -> float | None:
    aliases = {
        "objective_coverage": (
            "scores.objective_coverage",
            "score.objective_coverage",
            "objective_coverage",
        ),
        "required_coverage": (
            "scores.required_coverage",
            "score.required_coverage",
            "required_coverage",
            "scores.required_enforcement_coverage",
            "score.required_enforcement_coverage",
            "required_enforcement_coverage",
        ),
        "non_constraint_requirement_coverage": (
            "scores.non_constraint_requirement_coverage",
            "score.non_constraint_requirement_coverage",
            "non_constraint_requirement_coverage",
        ),
        "declared_quality_score": (
            "scores.declared_quality_score",
            "score.declared_quality_score",
            "declared_quality_score",
        ),
        "constraint_compliance": (
            "scores.constraint_compliance",
            "score.constraint_compliance",
            "constraint_compliance",
            "scores.constraint_adherence",
            "score.constraint_adherence",
            "constraint_adherence",
        ),
        "format_compliance": (
            "scores.format_compliance",
            "score.format_compliance",
            "format_compliance",
            "scores.response_format_compliance",
            "score.response_format_compliance",
            "response_format_compliance",
        ),
        "path_compliance": (
            "scores.path_compliance",
            "score.path_compliance",
            "path_compliance",
            "scores.path_scope_compliance",
            "score.path_scope_compliance",
            "path_scope_compliance",
        ),
        "artifact_contract": (
            "artifact_contract_pass",
            "scores.artifact_contract",
            "score.artifact_contract",
            "artifact_contract",
        ),
    }
    return _first_number(record, *aliases[name])


def _ratio(variant: float | None, baseline: float | None) -> float | None:
    if variant is None or baseline is None:
        return None
    if baseline == 0:
        return 1.0 if variant >= baseline else None
    return variant / baseline


def _mean_observed(values: Iterable[float | None]) -> float | None:
    observed = [value for value in values if value is not None]
    return mean(observed) if observed else None


def _token_cost(record: Mapping[str, Any]) -> float | None:
    total = _first_number(record, "usage.total_tokens", "total_tokens")
    if total is not None and total > 0:
        return total
    input_tokens = _first_number(record, "usage.input_tokens", "input_tokens")
    output_tokens = _first_number(record, "usage.output_tokens", "output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    observed = (input_tokens or 0.0) + (output_tokens or 0.0)
    return observed if observed > 0 else None


def _latency_cost(record: Mapping[str, Any]) -> float | None:
    direct = _first_number(
        record, "timing.elapsed_seconds", "elapsed_seconds", "elapsed"
    )
    if direct is not None:
        return direct
    stages = record.get("stages")
    if not isinstance(stages, Sequence) or isinstance(stages, (str, bytes)):
        return None
    observed = [
        value
        for stage in stages
        if isinstance(stage, Mapping)
        for value in [_first_number(stage, "elapsed_seconds", "duration_seconds")]
        if value is not None
    ]
    return sum(observed) if observed else None


@dataclass(frozen=True)
class CapabilityPolicy:
    """Thresholds for flagging meaningful paired regressions."""

    absolute_tolerance: float = 0.0
    quality_retention_floor: float = 1.0
    semantic_retention_floor: float = 1.0
    minimum_semantic_reviewers: int = 2
    cost_ratio_ceiling: float = 2.0
    latency_ratio_ceiling: float = 2.0


def _stable_key_value(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def pair_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return a stable pairing key independent of model-specific row layout."""

    return (
        record.get("executor"),
        record.get("model"),
        record.get("case_id", record.get("case")),
        record.get("repeat", record.get("run", 0)),
        _stable_key_value(record.get("sampling_signature", record.get("signature_config"))),
    )


def _semantic_evidence(
    record: Mapping[str, Any], counterpart_variant: Any
) -> Mapping[str, Any] | None:
    score = record.get("score", record.get("scores", {}))
    if isinstance(score, Mapping):
        reviews = score.get("semantic_reviews")
        if isinstance(reviews, Mapping):
            review = reviews.get(str(counterpart_variant))
            if isinstance(review, Mapping):
                return review
    value = _first_number(
        record,
        "scores.valid_information_retention",
        "score.valid_information_retention",
        "valid_information_retention",
    )
    if value is None:
        return None
    return {"status": "legacy", "overall": value, "reviewer_count": 0}


def pair_results(
    records: Sequence[Mapping[str, Any]], baseline_variant: str = "baseline"
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Pair rows with score evidence, even when the task contract itself failed."""

    baseline_groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    variants: list[Mapping[str, Any]] = []
    for record in records:
        score = record.get("score", record.get("scores"))
        if not isinstance(score, Mapping):
            continue
        if record.get("variant") == baseline_variant:
            baseline_groups.setdefault(pair_key(record), []).append(record)
        else:
            variants.append(record)
    return [
        (baseline_groups[pair_key(variant)][0], variant)
        for variant in variants
        if len(baseline_groups.get(pair_key(variant), [])) == 1
    ]


def score_capability_pair(
    baseline: Mapping[str, Any],
    variant: Mapping[str, Any],
    policy: CapabilityPolicy | None = None,
) -> dict[str, Any]:
    """Compute evidence-bounded capability retention for one paired result."""

    policy = policy or CapabilityPolicy()
    component_rows: dict[str, dict[str, Any]] = {}
    regressions: list[str] = []
    baseline_plan_fallback = bool(baseline.get("plan_fallback_used", False))
    variant_plan_fallback = bool(variant.get("plan_fallback_used", False))
    if variant_plan_fallback and not baseline_plan_fallback:
        regressions.append("plan_fallback")
    for name in QUALITY_COMPONENTS:
        baseline_value = _component(baseline, name)
        variant_value = _component(variant, name)
        delta = (
            variant_value - baseline_value
            if baseline_value is not None and variant_value is not None
            else None
        )
        missing_variant_evidence = baseline_value is not None and variant_value is None
        regression = missing_variant_evidence or (
            delta is not None and delta < -policy.absolute_tolerance
        )
        if regression:
            regressions.append(
                f"{name}:missing_variant_evidence" if missing_variant_evidence else name
            )
        component_rows[name] = {
            "baseline": baseline_value,
            "variant": variant_value,
            "delta": delta,
            "retention_ratio": _ratio(variant_value, baseline_value),
            "supported": baseline_value is not None and variant_value is not None,
            "missing_variant_evidence": missing_variant_evidence,
            "regression": regression,
        }

    quality_retention_ratio = _mean_observed(
        row["retention_ratio"] for row in component_rows.values()
    )
    non_constraint_requirement_retention = _mean_observed(
        component_rows[name]["retention_ratio"] for name in NON_CONSTRAINT_COMPONENTS
    )

    baseline_refusal = _observed_flag(
        baseline,
        "score.observations.unnecessary_refusal",
        "scores.observations.unnecessary_refusal",
        "observations.unnecessary_refusal",
        "unnecessary_refusal",
    )
    variant_refusal = _observed_flag(
        variant,
        "score.observations.unnecessary_refusal",
        "scores.observations.unnecessary_refusal",
        "observations.unnecessary_refusal",
        "unnecessary_refusal",
    )
    baseline_clarification = _observed_flag(
        baseline,
        "score.observations.unnecessary_clarification",
        "scores.observations.unnecessary_clarification",
        "observations.unnecessary_clarification",
        "unnecessary_clarification",
    )
    variant_clarification = _observed_flag(
        variant,
        "score.observations.unnecessary_clarification",
        "scores.observations.unnecessary_clarification",
        "observations.unnecessary_clarification",
        "unnecessary_clarification",
    )
    baseline_conservative = _observed_flag(
        baseline,
        "score.observations.over_conservative",
        "scores.observations.over_conservative",
        "observations.over_conservative",
        "over_conservative",
    )
    variant_conservative = _observed_flag(
        variant,
        "score.observations.over_conservative",
        "scores.observations.over_conservative",
        "observations.over_conservative",
        "over_conservative",
    )

    behavioral_regressions = {
        "unnecessary_refusal": (
            variant_refusal and not baseline_refusal
            if variant_refusal is not None and baseline_refusal is not None
            else None
        ),
        "unnecessary_clarification": (
            variant_clarification and not baseline_clarification
            if variant_clarification is not None
            and baseline_clarification is not None
            else None
        ),
        "over_conservative": (
            variant_conservative and not baseline_conservative
            if variant_conservative is not None
            and baseline_conservative is not None
            else None
        ),
    }
    regressions.extend(
        name for name, regressed in behavioral_regressions.items() if regressed
    )

    baseline_cost = _token_cost(baseline)
    variant_cost = _token_cost(variant)
    baseline_latency = _latency_cost(baseline)
    variant_latency = _latency_cost(variant)
    cost_ratio = _ratio(variant_cost, baseline_cost)
    latency_ratio = _ratio(variant_latency, baseline_latency)

    efficiency_regressions: list[str] = []
    if cost_ratio is not None and cost_ratio > policy.cost_ratio_ceiling:
        efficiency_regressions.append("cost")
    if latency_ratio is not None and latency_ratio > policy.latency_ratio_ceiling:
        efficiency_regressions.append("latency")
    if (
        quality_retention_ratio is not None
        and quality_retention_ratio < policy.quality_retention_floor
        and "quality" not in regressions
    ):
        regressions.append("quality")

    baseline_evidence = _semantic_evidence(baseline, variant.get("variant"))
    variant_evidence = _semantic_evidence(variant, baseline.get("variant"))
    baseline_information = (
        _number(baseline_evidence.get("overall"))
        if isinstance(baseline_evidence, Mapping)
        else None
    )
    variant_information = (
        _number(variant_evidence.get("overall"))
        if isinstance(variant_evidence, Mapping)
        else None
    )
    accepted_statuses = {"accepted", "adjudicated"}
    baseline_reviewed = bool(
        isinstance(baseline_evidence, Mapping)
        and baseline_evidence.get("status") in accepted_statuses
        and int(baseline_evidence.get("reviewer_count", 0) or 0)
        >= policy.minimum_semantic_reviewers
    )
    variant_reviewed = bool(
        isinstance(variant_evidence, Mapping)
        and variant_evidence.get("status") in accepted_statuses
        and int(variant_evidence.get("reviewer_count", 0) or 0)
        >= policy.minimum_semantic_reviewers
    )
    information_ratio = _ratio(variant_information, baseline_information)
    semantic_status = (
        "supported"
        if baseline_reviewed and variant_reviewed
        else "partial"
        if baseline_evidence is not None or variant_evidence is not None
        else "unsupported"
    )
    semantic_regression = (
        information_ratio is not None
        and information_ratio < policy.semantic_retention_floor
    )
    if semantic_regression:
        regressions.append("semantic_quality")

    return {
        "pair": {
            "executor": variant.get("executor"),
            "model": variant.get("model"),
            "case_id": variant.get("case_id", variant.get("case")),
            "repeat": variant.get("repeat", variant.get("run", 0)),
            "sampling_signature": variant.get("sampling_signature", variant.get("signature_config")),
            "baseline_variant": baseline.get("variant"),
            "variant": variant.get("variant"),
        },
        "quality_retention_ratio": quality_retention_ratio,
        "non_constraint_requirement_retention": non_constraint_requirement_retention,
        "component_retention": component_rows,
        "behavioral_regressions": behavioral_regressions,
        "plan_fallback_used": variant_plan_fallback,
        "plan_fallback_regression": variant_plan_fallback and not baseline_plan_fallback,
        "valid_information_retention": {
            "status": semantic_status,
            "value": information_ratio,
            "regression": semantic_regression,
            "note": "Only explicit evaluator observations are supported; regex scoring cannot establish general semantic preservation.",
        },
        "cost_ratio": cost_ratio,
        "latency_ratio": latency_ratio,
        "cost": {"baseline": baseline_cost, "variant": variant_cost},
        "latency": {"baseline": baseline_latency, "variant": variant_latency},
        "capability_regression_hit": bool(regressions),
        "capability_regression_reasons": sorted(set(regressions)),
        "efficiency_regression_hit": bool(efficiency_regressions),
        "efficiency_regression_reasons": efficiency_regressions,
        "supported_component_count": sum(
            row["supported"] for row in component_rows.values()
        ),
    }


def aggregate_capability_metrics(
    records: Sequence[Mapping[str, Any]],
    baseline_variant: str = "baseline",
    policy: CapabilityPolicy | None = None,
) -> dict[str, Any]:
    """Aggregate paired retention without treating missing pairs as failures."""

    pairs = pair_results(records, baseline_variant=baseline_variant)
    scored = [score_capability_pair(base, variant, policy) for base, variant in pairs]
    expected_variants = [
        record for record in records if record.get("variant") != baseline_variant
    ]
    row_counts: dict[tuple[tuple[Any, ...], Any], int] = {}
    for record in records:
        identity = (pair_key(record), record.get("variant"))
        row_counts[identity] = row_counts.get(identity, 0) + 1
    duplicate_rows = sum(count - 1 for count in row_counts.values() if count > 1)
    ambiguous_baseline_keys = {
        identity[0]
        for identity, count in row_counts.items()
        if identity[1] == baseline_variant and count > 1
    }

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        def aggregate_ratio(metric: str) -> float | None:
            observed = [
                item[metric]
                for item in items
                if item[metric]["baseline"] is not None
                and item[metric]["variant"] is not None
            ]
            if not observed:
                return None
            baseline_total = sum(item["baseline"] for item in observed)
            variant_total = sum(item["variant"] for item in observed)
            return _ratio(variant_total, baseline_total)

        hits = sum(item["capability_regression_hit"] for item in items)
        efficiency_hits = sum(item["efficiency_regression_hit"] for item in items)
        components: dict[str, dict[str, Any]] = {}
        for name in QUALITY_COMPONENTS:
            component_rows = [
                item["component_retention"][name]
                for item in items
            ]
            observed = [row for row in component_rows if row["supported"]]
            regression_hits = sum(row["regression"] for row in component_rows)
            components[name] = {
                "observed_pairs": len(observed),
                "missing_variant_evidence_hits": sum(
                    row["missing_variant_evidence"] for row in component_rows
                ),
                "retention_ratio": _mean_observed(
                    row["retention_ratio"] for row in observed
                ),
                "regression_hits": regression_hits,
                "regression_rate": (
                    regression_hits / len(component_rows) if component_rows else None
                ),
            }
        behavior: dict[str, dict[str, Any]] = {}
        for name in (
            "unnecessary_refusal",
            "unnecessary_clarification",
            "over_conservative",
        ):
            observed = [
                item["behavioral_regressions"][name]
                for item in items
                if item["behavioral_regressions"][name] is not None
            ]
            behavior[name] = {
                "observed_pairs": len(observed),
                "regression_hits": sum(observed),
                "regression_rate": sum(observed) / len(observed) if observed else None,
            }
        information_values = [
            item["valid_information_retention"]["value"]
            for item in items
            if item["valid_information_retention"]["status"] == "supported"
        ]
        semantic_observed = len(information_values)
        semantic_regression_hits = sum(
            bool(item["valid_information_retention"]["regression"])
            for item in items
        )
        return {
            "paired_rows": len(items),
            "quality_retention_ratio": _mean_observed(
                item["quality_retention_ratio"] for item in items
            ),
            "non_constraint_requirement_retention": _mean_observed(
                item["non_constraint_requirement_retention"] for item in items
            ),
            "capability_regression_hits": hits,
            "capability_regression_hit": bool(hits),
            "capability_regression_rate": hits / len(items) if items else None,
            "component_retention": components,
            "efficiency_regression_hits": efficiency_hits,
            "efficiency_regression_hit": bool(efficiency_hits),
            "efficiency_regression_rate": (
                efficiency_hits / len(items) if items else None
            ),
            "behavioral_regressions": behavior,
            "valid_information_retention": _mean_observed(information_values),
            "semantic_reviewed_pairs": semantic_observed,
            "semantic_review_coverage": (
                semantic_observed / len(items) if items else None
            ),
            "semantic_regression_hits": semantic_regression_hits,
            "semantic_regression_rate": (
                semantic_regression_hits / semantic_observed
                if semantic_observed
                else None
            ),
            "semantic_capability_status": (
                "supported"
                if items and semantic_observed == len(items)
                else "partial"
                if items
                else "unsupported"
            ),
            "cost_ratio": aggregate_ratio("cost"),
            "latency_ratio": aggregate_ratio("latency"),
            "plan_fallback_rows": sum(item["plan_fallback_used"] for item in items),
            "plan_fallback_rate": (
                sum(item["plan_fallback_used"] for item in items) / len(items)
                if items else None
            ),
            "plan_fallback_regression_hits": sum(
                item["plan_fallback_regression"] for item in items
            ),
            "plan_fallback_regression_rate": (
                sum(item["plan_fallback_regression"] for item in items) / len(items)
                if items else None
            ),
        }

    aggregate = summarize(scored)
    variant_names = sorted(
        {str(record.get("variant")) for record in expected_variants if record.get("variant")}
    )
    by_variant: dict[str, dict[str, Any]] = {}
    for variant in variant_names:
        metrics = summarize(
            [item for item in scored if item["pair"]["variant"] == variant]
        )
        eligible_rows = sum(record.get("variant") == variant for record in expected_variants)
        metrics["eligible_rows"] = eligible_rows
        metrics["missing_pair_rows"] = eligible_rows - metrics["paired_rows"]
        metrics["pair_coverage"] = (
            metrics["paired_rows"] / eligible_rows if eligible_rows else None
        )
        variant_row_counts = {
            key: count
            for key, count in row_counts.items()
            if key[1] == variant
        }
        metrics["duplicate_rows"] = sum(
            count - 1 for count in variant_row_counts.values() if count > 1
        )
        metrics["ambiguous_baseline_pairs"] = sum(
            pair_key(record) in ambiguous_baseline_keys
            for record in expected_variants
            if record.get("variant") == variant
        )
        by_variant[variant] = metrics
    return {
        "baseline_variant": baseline_variant,
        "eligible_variant_rows": len(expected_variants),
        **aggregate,
        "missing_pair_rows": len(expected_variants) - len(scored),
        "pair_coverage": len(scored) / len(expected_variants) if expected_variants else None,
        "duplicate_rows": duplicate_rows,
        "ambiguous_baseline_keys": len(ambiguous_baseline_keys),
        "semantic_capability_status": (
            "supported"
            if scored and all(
                item["valid_information_retention"]["status"] == "supported"
                for item in scored
            )
            else "partial"
            if scored
            else "unsupported"
        ),
        "by_variant": by_variant,
        "pairs": scored,
    }


def evaluate_capability_acceptance(
    summary: Mapping[str, Any],
    candidate_variants: Sequence[str],
    quality_retention_floor: float = 1.0,
    semantic_retention_floor: float = 1.0,
    require_semantic_review: bool = False,
    cost_ratio_ceiling: float = 1.0,
    latency_ratio_ceiling: float = 2.0,
    require_efficiency_evidence: bool = True,
) -> dict[str, Any]:
    """Evaluate final candidates without making ablations release gates."""

    by_variant = summary.get("by_variant", {})
    failures: list[dict[str, Any]] = []
    evaluated: list[str] = []
    for variant in candidate_variants:
        if variant not in by_variant:
            failures.append({"variant": variant, "reasons": ["missing_candidate"]})
            continue
        evaluated.append(variant)
        metrics = by_variant[variant]
        quality = metrics.get("quality_retention_ratio")
        non_constraint = metrics.get("non_constraint_requirement_retention")
        cost_ratio = metrics.get("cost_ratio")
        latency_ratio = metrics.get("latency_ratio")
        reasons: list[str] = []
        if not metrics.get("paired_rows"):
            reasons.append("missing_pairs")
        if metrics.get("missing_pair_rows") or not metrics.get("eligible_rows"):
            reasons.append("incomplete_pair_coverage")
        if metrics.get("duplicate_rows"):
            reasons.append("duplicate_pair_rows")
        if metrics.get("ambiguous_baseline_pairs"):
            reasons.append("ambiguous_baseline_pairs")
        if quality is None or quality < quality_retention_floor:
            reasons.append("quality_retention")
        if non_constraint is None or non_constraint < quality_retention_floor:
            reasons.append("non_constraint_requirement_retention")
        if metrics.get("capability_regression_hit"):
            reasons.append("paired_regression")
        if require_efficiency_evidence and (
            cost_ratio is None or latency_ratio is None
        ):
            reasons.append("missing_efficiency_evidence")
        if cost_ratio is not None and cost_ratio > cost_ratio_ceiling:
            reasons.append("token_cost_ratio")
        if latency_ratio is not None and latency_ratio > latency_ratio_ceiling:
            reasons.append("latency_ratio")
        if require_semantic_review:
            if metrics.get("semantic_review_coverage") != 1.0:
                reasons.append("incomplete_semantic_review_coverage")
            semantic_retention = metrics.get("valid_information_retention")
            if (
                semantic_retention is None
                or semantic_retention < semantic_retention_floor
            ):
                reasons.append("semantic_quality_retention")
            if metrics.get("semantic_regression_hits"):
                reasons.append("semantic_regression")
        if reasons:
            failures.append({"variant": variant, "reasons": reasons})
    status = (
        "unsupported"
        if not candidate_variants
        else "fail"
        if failures
        else "pass"
    )
    return {
        "status": status,
        "candidate_variants": evaluated,
        "quality_retention_floor": quality_retention_floor,
        "semantic_retention_floor": semantic_retention_floor,
        "semantic_review_required": require_semantic_review,
        "cost_ratio_ceiling": cost_ratio_ceiling,
        "latency_ratio_ceiling": latency_ratio_ceiling,
        "efficiency_evidence_required": require_efficiency_evidence,
        "failures": failures,
    }
