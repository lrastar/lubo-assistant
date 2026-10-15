"""Score complete local ASR runs against an independently prepared manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lubo.evaluation import score_subtitles, score_text
from lubo.subtitles import Cue


def evaluate(manifest: dict, candidate: dict) -> dict:
    if not candidate.get("complete"):
        raise ValueError("Recognition is still running; partial runs cannot be scored as complete")
    by_id = {item["id"]: item for item in candidate.get("samples", [])}
    if len(by_id) != len(candidate.get("samples", [])):
        raise ValueError("Duplicate sample IDs in candidate")
    rows = []
    total_chars = 0
    total_errors = 0
    rendered_errors = 0
    rendered_samples = 0
    failures = []
    for reference in manifest["samples"]:
        item = by_id.get(reference["id"])
        if not item or "error" in item:
            failures.append(reference["id"])
            continue
        if reference.get("audio_sha256") != item.get("audio_sha256"):
            raise ValueError(f"Different audio used for {reference['id']}")
        if reference.get("reference_origin") not in ("dataset", "human_reviewed"):
            raise ValueError("Reference must be independent, not unreviewed model output")
        score = score_text(reference["reference_text"], item.get("text", ""))
        total_chars += score["reference_chars"]
        total_errors += score["substitutions"] + score["deletions"] + score["insertions"]
        row = {"id": reference["id"], "text": score, "reference": reference["reference_text"],
               "hypothesis": item.get("text", ""), "audio_seconds": item.get("duration"),
               "inference_seconds": item.get("elapsed_seconds")}
        if item.get("cues") is not None:
            rendered = score_text(reference["reference_text"], " ".join(cue["text"] for cue in item["cues"]))
            row["rendered_text"] = rendered
            rendered_errors += rendered["substitutions"] + rendered["deletions"] + rendered["insertions"]
            rendered_samples += 1
        if reference.get("reference_cues") is not None and item.get("cues") is not None:
            ref_cues = [Cue(**cue) for cue in reference["reference_cues"]]
            hyp_cues = [Cue(**cue) for cue in item["cues"]]
            row["subtitles"] = score_subtitles(ref_cues, hyp_cues)
        rows.append(row)
    return {"benchmark": manifest["name"], "engine": candidate["engine"],
            "run_ok": candidate.get("ok", False), "run_error": candidate.get("error"),
            "model": candidate.get("model"), "runtime": candidate.get("runtime"),
            "samples_expected": len(manifest["samples"]), "samples_scored": len(rows),
            "failed_samples": failures, "complete_coverage": not failures,
            "reference_chars_scored": total_chars,
            "corpus_cer": total_errors / total_chars if total_chars and not failures else None,
            "scored_subset_cer": total_errors / total_chars if total_chars else None,
            "rendered_corpus_cer": (rendered_errors / total_chars
                                    if total_chars and not failures and rendered_samples == len(rows) else None),
            "timing_reference_available": any("subtitles" in row for row in rows),
            "jianying_comparison_available": False, "rows": rows,
            "limitations": manifest.get("limitations", "")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    result = evaluate(manifest, candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
