"""Prepare and apply blinded baseline/candidate capability reviews."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

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
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [row for row in payload.get("results", []) if row.get("success")]
    baselines = {pair_key(row): row for row in rows if row.get("variant") == "baseline"}
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
        {"schema_version": "1.0", "blinded": True, "dimensions": list(DIMENSIONS), "reviews": review_tasks},
        {"schema_version": "1.0", "seed_digest": hashlib.sha256(seed.encode()).hexdigest(), "mappings": mappings},
    )


def _matches(record: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return all(record.get(key) == value for key, value in identity.items())


def _normalized_score(scores: Mapping[str, Any]) -> float:
    values: list[float] = []
    for dimension in DIMENSIONS:
        value = scores.get(dimension)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 5:
            raise ValueError(f"all review scores must be numbers from 1 to 5: {dimension}")
        values.append(float(value))
    return sum(values) / (len(values) * 5)


def apply_reviews(payload: Mapping[str, Any], reviews: Mapping[str, Any], key: Mapping[str, Any]) -> dict[str, Any]:
    completed = {item["review_id"]: item for item in reviews.get("reviews", [])}
    mappings = {item["review_id"]: item for item in key.get("mappings", [])}
    enriched = json.loads(json.dumps(payload))
    rows = enriched.get("results", [])
    applied = 0
    for review_id, mapping in mappings.items():
        review = completed.get(review_id)
        if review is None:
            continue
        candidate_side = mapping["candidate_side"]
        baseline_side = "b" if candidate_side == "a" else "a"
        candidate_score = _normalized_score(review["scores"][candidate_side])
        baseline_score = _normalized_score(review["scores"][baseline_side])
        found = 0
        for row, identity, value in (
            (item, mapping["baseline"], baseline_score) for item in rows if _matches(item, mapping["baseline"])
        ):
            row.setdefault("score", {})["valid_information_retention"] = value
            row["score"]["semantic_capability_status"] = "partial"
            found += 1
        for row, identity, value in (
            (item, mapping["candidate"], candidate_score) for item in rows if _matches(item, mapping["candidate"])
        ):
            row.setdefault("score", {})["valid_information_retention"] = value
            row["score"]["semantic_capability_status"] = "partial"
            found += 1
        if found != 2:
            raise ValueError(f"review mapping did not resolve exactly two rows: {review_id}")
        applied += 1
    enriched["pairwise_review"] = {"applied": applied, "available": len(mappings), "blinded": True}
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
    prepare.add_argument("--seed", required=True)
    prepare.add_argument("--cases", type=Path)
    prepare.add_argument("--reviews", type=Path, required=True)
    prepare.add_argument("--key", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--results", type=Path, required=True)
    apply.add_argument("--reviews", type=Path, required=True)
    apply.add_argument("--key", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prompts: dict[str, str] = {}
        if args.cases:
            cases = json.loads(args.cases.read_text(encoding="utf-8"))
            prompts = {str(case["id"]): str(case["prompt"]) for case in cases}
        reviews, key = prepare_reviews(_load(args.results), args.experiment_root, set(args.variant), args.seed, prompts)
        _write(args.reviews, reviews)
        _write(args.key, key)
    else:
        _write(args.output, apply_reviews(_load(args.results), _load(args.reviews), _load(args.key)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

