"""Paired capability-retention metrics for baseline and skill variants.

The metrics in this module are deliberately evidence-bounded. Missing or
unsupported observations are excluded from denominators instead of being
coerced to zero, and broad semantic quality is never inferred from regexes.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


QUALITY_COMPONENTS = (
    "objective_coverage",
    "required_coverage",
    "constraint_compliance",
    "format_compliance",
    "path_compliance",
    "artifact_contract",
)

NON_CONSTRAINT_COMPONENTS = (
    "objective_coverage",
    "required_coverage",
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
    if total is not None:
        return total
    input_tokens = _first_number(record, "usage.input_tokens", "input_tokens")
    output_tokens = _first_number(record, "usage.output_tokens", "output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0.0) + (output_tokens or 0.0)


@dataclass(frozen=True)
class CapabilityPolicy:
    """Thresholds for flagging meaningful paired regressions."""

    absolute_tolerance: float = 0.05
    quality_retention_floor: float = 0.95
    cost_ratio_ceiling: float = 2.0
    latency_ratio_ceiling: float = 2.0


def pair_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return a stable pairing key independent of model-specific row layout."""

    return (
        record.get("executor"),
        record.get("model"),
        record.get("case_id", record.get("case")),
        record.get("repeat", record.get("run", 0)),
        record.get("sampling_signature", record.get("signature_config")),
    )


def pair_results(
    records: Sequence[Mapping[str, Any]], baseline_variant: str = "baseline"
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Pair successful baseline/variant rows; incomplete rows are not fabricated."""

    baselines: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    variants: list[Mapping[str, Any]] = []
    for record in records:
        if record.get("success") is False:
            continue
        if record.get("variant") == baseline_variant:
            baselines[pair_key(record)] = record
        else:
            variants.append(record)
    return [
        (baselines[pair_key(variant)], variant)
        for variant in variants
        if pair_key(variant) in baselines
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
    for name in QUALITY_COMPONENTS:
        baseline_value = _component(baseline, name)
        variant_value = _component(variant, name)
        delta = (
            variant_value - baseline_value
            if baseline_value is not None and variant_value is not None
            else None
        )
        regression = delta is not None and delta < -policy.absolute_tolerance
        if regression:
            regressions.append(name)
        component_rows[name] = {
            "baseline": baseline_value,
            "variant": variant_value,
            "delta": delta,
            "retention_ratio": _ratio(variant_value, baseline_value),
            "supported": baseline_value is not None and variant_value is not None,
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
    baseline_latency = _first_number(
        baseline, "timing.elapsed_seconds", "elapsed_seconds", "elapsed"
    )
    variant_latency = _first_number(
        variant, "timing.elapsed_seconds", "elapsed_seconds", "elapsed"
    )
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

    baseline_information = _first_number(
        baseline,
        "scores.valid_information_retention",
        "score.valid_information_retention",
        "valid_information_retention",
    )
    variant_information = _first_number(
        variant,
        "scores.valid_information_retention",
        "score.valid_information_retention",
        "valid_information_retention",
    )

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
        "valid_information_retention": {
            "status": (
                "partial"
                if baseline_information is not None and variant_information is not None
                else "unsupported"
            ),
            "value": _ratio(variant_information, baseline_information),
            "note": "Only explicit evaluator observations are supported; regex scoring cannot establish general semantic preservation.",
        },
        "cost_ratio": cost_ratio,
        "latency_ratio": latency_ratio,
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

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        hits = sum(item["capability_regression_hit"] for item in items)
        efficiency_hits = sum(item["efficiency_regression_hit"] for item in items)
        components: dict[str, dict[str, Any]] = {}
        for name in QUALITY_COMPONENTS:
            observed = [
                item["component_retention"][name]
                for item in items
                if item["component_retention"][name]["supported"]
            ]
            regression_hits = sum(row["regression"] for row in observed)
            components[name] = {
                "observed_pairs": len(observed),
                "retention_ratio": _mean_observed(
                    row["retention_ratio"] for row in observed
                ),
                "regression_hits": regression_hits,
                "regression_rate": (
                    regression_hits / len(observed) if observed else None
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
            item["valid_information_retention"]["value"] for item in items
        ]
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
            "cost_ratio": _mean_observed(item["cost_ratio"] for item in items),
            "latency_ratio": _mean_observed(item["latency_ratio"] for item in items),
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
        by_variant[variant] = metrics
    return {
        "baseline_variant": baseline_variant,
        "eligible_variant_rows": len(expected_variants),
        **aggregate,
        "missing_pair_rows": len(expected_variants) - len(scored),
        "pair_coverage": len(scored) / len(expected_variants) if expected_variants else None,
        "semantic_capability_status": "partial" if scored else "unsupported",
        "by_variant": by_variant,
        "pairs": scored,
    }


def evaluate_capability_acceptance(
    summary: Mapping[str, Any],
    candidate_variants: Sequence[str],
    quality_retention_floor: float = 0.95,
) -> dict[str, Any]:
    """Evaluate final candidates without making ablations release gates."""

    by_variant = summary.get("by_variant", {})
    evaluated = [variant for variant in candidate_variants if variant in by_variant]
    failures: list[dict[str, Any]] = []
    for variant in evaluated:
        metrics = by_variant[variant]
        quality = metrics.get("quality_retention_ratio")
        non_constraint = metrics.get("non_constraint_requirement_retention")
        reasons: list[str] = []
        if not metrics.get("paired_rows"):
            reasons.append("missing_pairs")
        if metrics.get("pair_coverage") != 1.0:
            reasons.append("incomplete_pair_coverage")
        if quality is None or quality < quality_retention_floor:
            reasons.append("quality_retention")
        if non_constraint is None or non_constraint < quality_retention_floor:
            reasons.append("non_constraint_requirement_retention")
        if metrics.get("capability_regression_hit"):
            reasons.append("paired_regression")
        if reasons:
            failures.append({"variant": variant, "reasons": reasons})
    status = "unsupported" if not evaluated else ("fail" if failures else "pass")
    return {
        "status": status,
        "candidate_variants": evaluated,
        "quality_retention_floor": quality_retention_floor,
        "failures": failures,
    }
