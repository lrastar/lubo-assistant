"""Compare complete WenetSpeech runs using paired, split-stratified blocks."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import random

from evaluate_asr import evaluate


def compare(manifest: dict, baseline: dict, candidate: dict,
            *, seed: int = 20261008, replicates: int = 10000) -> dict:
    if replicates < 100:
        raise ValueError("Use at least 100 bootstrap replicates")
    sample_ids = [sample["id"] for sample in manifest["samples"]]
    if not sample_ids or len(sample_ids) != len(set(sample_ids)):
        raise ValueError("Manifest must contain unique, nonempty sample IDs")
    scores = []
    for run in (baseline, candidate):
        if run.get("ok") is not True:
            raise ValueError("Both runs must have completed successfully")
        if {item["id"] for item in run.get("samples", [])} != set(sample_ids):
            raise ValueError("Runs must contain exactly the manifest samples")
        if any(not sample.get("audio_sha256") for sample in manifest["samples"]):
            raise ValueError("Manifest must pin the audio SHA-256 of every sample")
        score = evaluate(manifest, run)
        if not score["complete_coverage"] or score["corpus_cer"] is None:
            raise ValueError("Partial coverage cannot pass a model comparison")
        scores.append(score)

    def errors(row: dict) -> int:
        return sum(row["text"][key] for key in ("substitutions", "deletions", "insertions"))

    base_rows, candidate_rows = ({row["id"]: row for row in score["rows"]}
                                 for score in scores)
    splits = defaultdict(list)
    outcomes = {"improved": 0, "same": 0, "worse": 0}
    for sample in manifest["samples"]:
        source = sample.get("source", {})
        if not isinstance(source.get("split"), str) or not isinstance(source.get("row"), int):
            raise ValueError("This comparator requires fixed Wenet split/row metadata")
        sample_id = sample["id"]
        base_errors = errors(base_rows[sample_id])
        new_errors = errors(candidate_rows[sample_id])
        chars = base_rows[sample_id]["text"]["reference_chars"]
        delta = base_errors - new_errors
        outcomes["improved" if delta > 0 else "worse" if delta < 0 else "same"] += 1
        splits[source["split"]].append((source["row"], chars, base_errors, new_errors))

    strata = {}
    for split, rows in sorted(splits.items()):
        rows.sort()
        if len(rows) % 4:
            raise ValueError("Each split must contain complete groups of four")
        groups = []
        for start in range(0, len(rows), 4):
            block = rows[start:start + 4]
            if [row[0] for row in block] != list(range(block[0][0], block[0][0] + 4)):
                raise ValueError("Bootstrap blocks must be four adjacent source rows")
            groups.append(tuple(sum(row[index] for row in block) for index in (1, 2, 3)))
        strata[split] = groups

    rng = random.Random(seed)
    reductions = []
    for _ in range(replicates):
        chars = delta = 0
        for groups in strata.values():
            for _ in groups:
                count, base_errors, new_errors = rng.choice(groups)
                chars += count
                delta += base_errors - new_errors
        reductions.append(100 * delta / chars)
    reductions.sort()

    def percentile(fraction: float) -> float:
        position = (len(reductions) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(reductions) - 1)
        return reductions[lower] + (reductions[upper] - reductions[lower]) * (position - lower)

    total_base = sum(errors(row) for row in base_rows.values())
    total_new = sum(errors(row) for row in candidate_rows.values())
    return {
        "benchmark": manifest["name"], "samples": len(sample_ids),
        "baseline_engine": baseline["engine"], "candidate_engine": candidate["engine"],
        "reference_chars": scores[0]["reference_chars_scored"],
        "baseline_errors": total_base, "candidate_errors": total_new,
        "baseline_cer": scores[0]["corpus_cer"], "candidate_cer": scores[1]["corpus_cer"],
        "relative_error_reduction": (total_base - total_new) / total_base if total_base else None,
        "baseline_rendered_cer": scores[0]["rendered_corpus_cer"],
        "candidate_rendered_cer": scores[1]["rendered_corpus_cer"],
        "outcomes": outcomes,
        "cer_reduction_percentage_points_95ci": [percentile(.025), percentile(.975)],
        "seed": seed, "bootstrap_replicates": replicates,
        "bootstrap_unit": "4 adjacent utterances; stratified by test split",
        "groups_per_split": {split: len(groups) for split, groups in strata.items()},
        "limitations": "Selected public samples only. Does not establish accuracy on the user's recordings or subtitle timing quality.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "baseline", "candidate", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261008)
    parser.add_argument("--replicates", type=int, default=10000)
    args = parser.parse_args()
    result = compare(*(json.loads(path.read_text(encoding="utf-8-sig"))
                       for path in (args.manifest, args.baseline, args.candidate)),
                     seed=args.seed, replicates=args.replicates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
