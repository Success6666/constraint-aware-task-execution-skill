"""Prepare and apply blinded baseline/candidate capability reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from capability_metrics import pair_key


DIMENSIONS = ("correctness", "completeness", "usefulness", "requirement_retention")


def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "executor": record.get("executor"),
        "model": record.get("model"),
        "case_id": record.get("case_id", record.get("case")),
        "repeat": record.get("repeat", record.get("run", 0)),
        "sampling_signature": record.get("sampling_signature", record.get("signature_config")),
        "variant": record.get("variant"),
    }


def _answer(record: Mapping[str, Any], root: Path) -> str:
    evidence = record.get("evidence", {})
    relative = evidence.get("answer") if isinstance(evidence, Mapping) else None
    if not relative:
        raise ValueError(f"missing answer evidence for {_identity(record)}")
    path = (root / str(relative)).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"answer evidence escapes experiment root: {relative}")
    return path.read_text(encoding="utf-8")


def prepare_reviews(
    payload: Mapping[str, Any],
    experiment_root: Path,
    variants: set[str],
    seed: str,
    prompts: Mapping[str, str] | None = None,
    reviewer_id: str = "reviewer-unknown",
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [row for row in payload.get("results", []) if row.get("success")]
    baseline_groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("variant") == "baseline":
            baseline_groups.setdefault(pair_key(row), []).append(row)
    ambiguous = [key for key, group in baseline_groups.items() if len(group) != 1]
    if ambiguous:
        raise ValueError(f"ambiguous baseline rows prevent blinded review: {len(ambiguous)}")
    baselines = {key: group[0] for key, group in baseline_groups.items()}
    review_tasks: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for candidate in rows:
        if candidate.get("variant") not in variants:
            continue
        baseline = baselines.get(pair_key(candidate))
        if baseline is None:
            continue
        identity = _identity(candidate)
        review_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:20]
        candidate_side = "a" if int(hashlib.sha256(f"{seed}:{review_id}".encode()).hexdigest(), 16) % 2 == 0 else "b"
        responses = {"baseline": _answer(baseline, experiment_root), "candidate": _answer(candidate, experiment_root)}
        side_to_name = {candidate_side: "candidate", "b" if candidate_side == "a" else "a": "baseline"}
        review_tasks.append({
            "review_id": review_id,
            "case_id": identity["case_id"],
            "prompt": (prompts or {}).get(str(identity["case_id"]), ""),
            "response_a": responses[side_to_name["a"]],
            "response_b": responses[side_to_name["b"]],
            "scores": {
                "a": {dimension: None for dimension in DIMENSIONS},
                "b": {dimension: None for dimension in DIMENSIONS},
            },
            "notes": "",
        })
        mappings.append({
            "review_id": review_id,
            "candidate_side": candidate_side,
            "baseline": _identity(baseline),
            "candidate": identity,
        })
    return (
        {
            "schema_version": "1.0",
            "reviewer_id": reviewer_id,
            "blinded": True,
            "dimensions": list(DIMENSIONS),
            "reviews": review_tasks,
        },
        {
            "schema_version": "1.0",
            "reviewer_id": reviewer_id,
            "seed_digest": hashlib.sha256(seed.encode()).hexdigest(),
            "mappings": mappings,
        },
    )


def _matches(record: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return _identity(record) == dict(identity)


def _normalized_dimensions(scores: Mapping[str, Any]) -> dict[str, float] | None:
    raw = [scores.get(dimension) for dimension in DIMENSIONS]
    if all(value is None for value in raw):
        return None
    if any(value is None for value in raw):
        raise ValueError("partially completed review side is not valid")
    values: dict[str, float] = {}
    for dimension in DIMENSIONS:
        value = scores.get(dimension)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 5:
            raise ValueError(f"all review scores must be numbers from 1 to 5: {dimension}")
        values[dimension] = float(value) / 5
    return values


def _review_score(
    review_id: str, reviewer_id: str, scores: Mapping[str, Any]
) -> dict[str, Any] | None:
    dimensions = _normalized_dimensions(scores)
    if dimensions is None:
        return None
    return {
        "review_id": review_id,
        "reviewer_id": reviewer_id,
        "overall": sum(dimensions.values()) / len(dimensions),
        "dimensions": dimensions,
    }


def _unique_by_id(items: Any, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(items, list):
        raise ValueError(f"{label} must be an array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("review_id"), str):
            raise ValueError(f"invalid {label} entry")
        review_id = item["review_id"]
        if review_id in indexed:
            raise ValueError(f"duplicate review_id in {label}: {review_id}")
        indexed[review_id] = item
    return indexed


def _average_scores(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions = {
        dimension: mean(float(score["dimensions"][dimension]) for score in scores)
        for dimension in DIMENSIONS
    }
    return {
        "overall": mean(float(score["overall"]) for score in scores),
        "dimensions": dimensions,
    }


def _disagreement(
    entries: Sequence[Mapping[str, Any]], threshold: float
) -> dict[str, Any]:
    dimensions: dict[str, Any] = {}
    disputed: list[str] = []
    for dimension in DIMENSIONS:
        deltas = [
            float(entry["candidate"]["dimensions"][dimension])
            - float(entry["baseline"]["dimensions"][dimension])
            for entry in entries
        ]
        spread = max(deltas) - min(deltas) if deltas else None
        directions = {
            -1 if delta < 0 else 1 if delta > 0 else 0
            for delta in deltas
        }
        direction_conflict = len(directions) > 1
        is_disputed = bool(
            deltas
            and (direction_conflict or (spread is not None and spread > threshold))
        )
        if is_disputed:
            disputed.append(dimension)
        dimensions[dimension] = {
            "candidate_minus_baseline": deltas,
            "spread": spread,
            "direction_conflict": direction_conflict,
            "disputed": is_disputed,
        }
    return {
        "threshold": threshold,
        "disputed": bool(disputed),
        "disputed_dimensions": disputed,
        "dimensions": dimensions,
    }


def _adjudication_scores(
    payload: Mapping[str, Any] | None,
) -> dict[str, tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]]]:
    if payload is None:
        return {}
    if payload.get("schema_version") != "1.0":
        raise ValueError("adjudication packet must use schema_version=1.0")
    items = _unique_by_id(payload.get("adjudications", []), "adjudications")
    result: dict[str, tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]]] = {}
    for review_id, item in items.items():
        adjudicator_id = item.get("adjudicator_id")
        if not isinstance(adjudicator_id, str) or not adjudicator_id:
            raise ValueError(f"missing adjudicator_id for {review_id}")
        baseline = _review_score(
            review_id, adjudicator_id, item.get("baseline_scores", {})
        )
        candidate = _review_score(
            review_id, adjudicator_id, item.get("candidate_scores", {})
        )
        if baseline is None or candidate is None:
            raise ValueError(f"incomplete adjudication for {review_id}")
        result[review_id] = (baseline, candidate, item)
    return result


def _aggregate_pair(
    review_id: str,
    entries: Sequence[Mapping[str, Any]],
    minimum_reviewers: int,
    disagreement_threshold: float,
    adjudication: tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    reviewer_ids = [str(entry["reviewer_id"]) for entry in entries]
    disagreement = _disagreement(entries, disagreement_threshold)
    status = "insufficient_reviews"
    if len(entries) >= minimum_reviewers:
        status = "needs_adjudication" if disagreement["disputed"] else "accepted"
    if adjudication is not None and status != "needs_adjudication":
        raise ValueError(
            f"adjudication is only valid for a disputed, fully reviewed pair: {review_id}"
        )
    if status == "needs_adjudication" and adjudication is not None:
        baseline, candidate, item = adjudication
        status = "adjudicated"
        baseline_aggregate = {
            "overall": baseline["overall"],
            "dimensions": baseline["dimensions"],
        }
        candidate_aggregate = {
            "overall": candidate["overall"],
            "dimensions": candidate["dimensions"],
        }
        adjudication_info: dict[str, Any] | None = {
            "adjudicator_id": item["adjudicator_id"],
            "notes": item.get("notes", ""),
        }
    else:
        baseline_aggregate = _average_scores([entry["baseline"] for entry in entries]) if entries else {"overall": None, "dimensions": {}}
        candidate_aggregate = _average_scores([entry["candidate"] for entry in entries]) if entries else {"overall": None, "dimensions": {}}
        adjudication_info = None
    shared = {
        "review_id": review_id,
        "reviewer_ids": reviewer_ids,
        "reviewer_count": len(entries),
        "minimum_reviewers": minimum_reviewers,
        "status": status,
        "disagreement": disagreement,
        "adjudication": adjudication_info,
    }
    return (
        {**shared, **baseline_aggregate},
        {**shared, **candidate_aggregate},
        status,
    )


def apply_reviews(
    payload: Mapping[str, Any],
    review_packets: Sequence[Mapping[str, Any]],
    key_packets: Sequence[Mapping[str, Any]],
    minimum_reviewers: int = 2,
    disagreement_threshold: float = 0.2,
    adjudications: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if len(review_packets) != len(key_packets) or not review_packets:
        raise ValueError("each review packet must have one separate mapping key")
    if minimum_reviewers < 2:
        raise ValueError("minimum_reviewers must be at least 2")
    if disagreement_threshold < 0:
        raise ValueError("disagreement_threshold must be non-negative")
    reviewer_ids: set[str] = set()
    pair_mappings: dict[str, Mapping[str, Any]] = {}
    pair_entries: dict[str, list[dict[str, Any]]] = {}
    for reviews, key in zip(review_packets, key_packets):
        if reviews.get("schema_version") != "1.0" or reviews.get("blinded") is not True:
            raise ValueError("review packet must be blinded schema_version=1.0")
        if tuple(reviews.get("dimensions", ())) != DIMENSIONS:
            raise ValueError("review dimensions do not match the capability contract")
        if key.get("schema_version") != "1.0":
            raise ValueError("review key must use schema_version=1.0")
        reviewer_id = reviews.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id:
            raise ValueError("review packet must declare reviewer_id")
        if reviewer_id != key.get("reviewer_id"):
            raise ValueError(f"reviewer_id does not match mapping key: {reviewer_id}")
        if reviewer_id in reviewer_ids:
            raise ValueError(f"duplicate reviewer_id: {reviewer_id}")
        reviewer_ids.add(reviewer_id)
        completed = _unique_by_id(reviews.get("reviews", []), f"reviews:{reviewer_id}")
        mappings = _unique_by_id(key.get("mappings", []), f"mappings:{reviewer_id}")
        unknown_reviews = sorted(set(completed) - set(mappings))
        if unknown_reviews:
            raise ValueError(f"review packet contains unknown review ids: {unknown_reviews}")
        for review_id, mapping in mappings.items():
            candidate_side = mapping.get("candidate_side")
            if candidate_side not in {"a", "b"}:
                raise ValueError(f"invalid candidate side for {review_id}")
            if pair_key(mapping["baseline"]) != pair_key(mapping["candidate"]):
                raise ValueError(f"review mapping does not describe a paired result: {review_id}")
            if mapping["baseline"].get("variant") == mapping["candidate"].get("variant"):
                raise ValueError(f"review mapping variants must differ: {review_id}")
            canonical = {
                "baseline": mapping["baseline"],
                "candidate": mapping["candidate"],
            }
            if review_id in pair_mappings and pair_mappings[review_id] != canonical:
                raise ValueError(f"reviewer packets disagree on pair identity: {review_id}")
            pair_mappings[review_id] = canonical
            review = completed.get(review_id)
            if review is None:
                continue
            baseline_side = "b" if candidate_side == "a" else "a"
            candidate_score = _review_score(
                review_id, reviewer_id, review["scores"][candidate_side]
            )
            baseline_score = _review_score(
                review_id, reviewer_id, review["scores"][baseline_side]
            )
            if candidate_score is None and baseline_score is None:
                continue
            if candidate_score is None or baseline_score is None:
                raise ValueError(f"both sides must be completed together: {review_id}")
            pair_entries.setdefault(review_id, []).append(
                {
                    "reviewer_id": reviewer_id,
                    "baseline": baseline_score,
                    "candidate": candidate_score,
                }
            )

    adjudication_scores = _adjudication_scores(adjudications)
    unknown_adjudications = sorted(set(adjudication_scores) - set(pair_mappings))
    if unknown_adjudications:
        raise ValueError(f"unknown adjudication ids: {unknown_adjudications}")
    for review_id, (_, _, item) in adjudication_scores.items():
        if item.get("adjudicator_id") in reviewer_ids:
            raise ValueError(
                f"adjudicator must be independent from the original reviewers: {review_id}"
            )
    enriched = json.loads(json.dumps(payload))
    rows = enriched.get("results", [])
    status_counts: dict[str, int] = {}
    pair_statuses: list[dict[str, Any]] = []
    for review_id, mapping in pair_mappings.items():
        baseline_score, candidate_score, status = _aggregate_pair(
            review_id,
            pair_entries.get(review_id, []),
            minimum_reviewers,
            disagreement_threshold,
            adjudication_scores.get(review_id),
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        pair_statuses.append({
            "review_id": review_id,
            "executor": mapping["candidate"].get("executor"),
            "model": mapping["candidate"].get("model"),
            "case_id": mapping["candidate"].get("case_id"),
            "repeat": mapping["candidate"].get("repeat"),
            "variant": mapping["candidate"].get("variant"),
            "status": status,
            "reviewer_count": candidate_score["reviewer_count"],
            "disputed_dimensions": candidate_score["disagreement"]["disputed_dimensions"],
        })
        found = 0
        for row, identity, aggregate, counterpart in (
            (row, mapping["baseline"], baseline_score, mapping["candidate"].get("variant"))
            for row in rows
            if _matches(row, mapping["baseline"])
        ):
            score = row.setdefault("score", {})
            score.setdefault("semantic_reviews", {})[str(counterpart)] = aggregate
            score["valid_information_retention"] = (
                aggregate["overall"] if status in {"accepted", "adjudicated"} else None
            )
            score["semantic_capability_status"] = (
                "supported" if status in {"accepted", "adjudicated"} else "partial"
            )
            found += 1
        for row, identity, aggregate, counterpart in (
            (row, mapping["candidate"], candidate_score, mapping["baseline"].get("variant"))
            for row in rows
            if _matches(row, mapping["candidate"])
        ):
            score = row.setdefault("score", {})
            score.setdefault("semantic_reviews", {})[str(counterpart)] = aggregate
            score["valid_information_retention"] = (
                aggregate["overall"] if status in {"accepted", "adjudicated"} else None
            )
            score["semantic_capability_status"] = (
                "supported" if status in {"accepted", "adjudicated"} else "partial"
            )
            found += 1
        if found != 2:
            raise ValueError(f"review mapping did not resolve exactly two rows: {review_id}")
    accepted = sum(
        status_counts.get(status, 0) for status in ("accepted", "adjudicated")
    )
    enriched["pairwise_review"] = {
        "reviewers": sorted(reviewer_ids),
        "minimum_reviewers": minimum_reviewers,
        "disagreement_threshold": disagreement_threshold,
        "available_pairs": len(pair_mappings),
        "accepted_pairs": accepted,
        "status_counts": status_counts,
        "pairs": pair_statuses,
        "complete": bool(pair_mappings) and accepted == len(pair_mappings),
        "blinded": True,
    }
    return enriched


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or apply blinded capability reviews.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--results", type=Path, required=True)
    prepare.add_argument("--experiment-root", type=Path, required=True)
    prepare.add_argument("--variant", action="append", required=True)
    seed_source = prepare.add_mutually_exclusive_group(required=True)
    seed_source.add_argument("--seed")
    seed_source.add_argument("--seed-file", type=Path)
    prepare.add_argument("--reviewer-id", required=True)
    prepare.add_argument("--cases", type=Path)
    prepare.add_argument("--reviews", type=Path, required=True)
    prepare.add_argument("--key", type=Path, required=True)
    prepare.add_argument("--force", action="store_true")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--results", type=Path, required=True)
    apply.add_argument("--reviews", type=Path, action="append", required=True)
    apply.add_argument("--key", type=Path, action="append", required=True)
    apply.add_argument("--adjudications", type=Path)
    apply.add_argument("--minimum-reviewers", type=int, default=2)
    apply.add_argument("--disagreement-threshold", type=float, default=0.2)
    apply.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        seed = args.seed
        if args.seed_file:
            seed = args.seed_file.read_text(encoding="utf-8").strip()
        if not seed:
            raise ValueError("review seed must not be empty")
        prompts: dict[str, str] = {}
        if args.cases:
            cases = json.loads(args.cases.read_text(encoding="utf-8"))
            prompts = {str(case["id"]): str(case["prompt"]) for case in cases}
        if not args.force and (args.reviews.exists() or args.key.exists()):
            raise ValueError("refusing to overwrite review packet or mapping key; use --force")
        reviews, key = prepare_reviews(
            _load(args.results),
            args.experiment_root,
            set(args.variant),
            seed,
            prompts,
            reviewer_id=args.reviewer_id,
        )
        _write(args.reviews, reviews)
        _write(args.key, key)
    else:
        enriched = apply_reviews(
            _load(args.results),
            [_load(path) for path in args.reviews],
            [_load(path) for path in args.key],
            minimum_reviewers=args.minimum_reviewers,
            disagreement_threshold=args.disagreement_threshold,
            adjudications=_load(args.adjudications) if args.adjudications else None,
        )
        _write(args.output, enriched)
        return 0 if enriched["pairwise_review"]["complete"] else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
