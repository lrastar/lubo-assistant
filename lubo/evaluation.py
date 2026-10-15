"""Transcript and subtitle quality metrics against an independent reference.

No model output is treated as ground truth. Timing scores include coverage so
unmatched text cannot make an inaccurate recognizer appear well synchronized.
"""
from __future__ import annotations

from bisect import bisect_right
import math
import unicodedata

from lubo.subtitles import Cue


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if not c.isspace()
                   and unicodedata.category(c)[0] not in "PCZ")


def _alignment(reference: str, hypothesis: str) -> tuple[dict, dict[int, int]]:
    from rapidfuzz.distance import Levenshtein

    operations = Levenshtein.editops(reference, hypothesis)
    counts = {"replace": 0, "delete": 0, "insert": 0}
    for op in operations:
        counts[op.tag] += 1
    matching = {}
    for op in operations.as_opcodes():
        if op.tag == "equal":
            matching.update((r, h) for r, h in zip(range(op.src_start, op.src_end),
                                                   range(op.dest_start, op.dest_end)))
    return {
        "reference_chars": len(reference),
        "hypothesis_chars": len(hypothesis),
        "substitutions": counts["replace"],
        "deletions": counts["delete"],
        "insertions": counts["insert"],
        "cer": len(operations) / len(reference) if reference else None,
        "silence_hallucination_chars": len(hypothesis) if not reference else 0,
        "matched_reference_fraction": len(matching) / len(reference) if reference else None,
    }, matching


def score_text(reference: str, hypothesis: str) -> dict:
    return _alignment(normalize_text(reference), normalize_text(hypothesis))[0]


def _flatten(cues: list[Cue]) -> tuple[str, list[int], list[Cue]]:
    text = ""
    starts = []
    kept = []
    for cue in cues:
        if not (math.isfinite(cue.start) and math.isfinite(cue.end)
                and 0 <= cue.start < cue.end):
            raise ValueError("Cannot evaluate an invalid subtitle interval")
        if kept and cue.start < kept[-1].start:
            raise ValueError("Subtitle order must follow its timeline")
        content = normalize_text(cue.text)
        if content:
            starts.append(len(text))
            kept.append(cue)
            text += content
    return text, starts, kept


def _error_summary(errors: list[float]) -> dict:
    if not errors:
        return {"count": 0, "mean_signed_ms": None, "median_absolute_ms": None,
                "p95_absolute_ms": None, "max_absolute_ms": None}
    values = sorted(abs(error) * 1000 for error in errors)

    def percentile(fraction: float) -> float:
        index = (len(values) - 1) * fraction
        left = math.floor(index)
        right = math.ceil(index)
        return values[left] + (values[right] - values[left]) * (index - left)

    return {"count": len(errors), "mean_signed_ms": sum(errors) * 1000 / len(errors),
            "median_absolute_ms": percentile(.5), "p95_absolute_ms": percentile(.95),
            "max_absolute_ms": values[-1]}


def score_subtitles(reference: list[Cue], hypothesis: list[Cue],
                    boundary_tolerance_chars: int = 1) -> dict:
    if boundary_tolerance_chars < 0:
        raise ValueError("Boundary tolerance cannot be negative")
    ref_text, ref_starts, ref_cues = _flatten(reference)
    hyp_text, hyp_starts, hyp_cues = _flatten(hypothesis)
    text_score, matches = _alignment(ref_text, hyp_text)
    reverse_matches = {h: r for r, h in matches.items()}
    ref_boundaries = ref_starts[1:]
    unused_boundaries = set(ref_boundaries)
    matched_boundaries = 0
    for boundary in hyp_starts[1:]:
        # The boundary is immediately after the preceding character. If that
        # character was not recognized, count a false positive instead of
        # silently excluding this boundary from the denominator.
        previous = reverse_matches.get(boundary - 1)
        if previous is None:
            continue
        candidates = [r for r in unused_boundaries
                      if abs(r - previous - 1) <= boundary_tolerance_chars]
        if candidates:
            chosen = min(candidates, key=lambda r: (abs(r - previous - 1), r))
            unused_boundaries.remove(chosen)
            matched_boundaries += 1
    predicted_count = max(0, len(hyp_starts) - 1)
    reference_count = len(ref_boundaries)
    precision = matched_boundaries / predicted_count if predicted_count else float(reference_count == 0)
    recall = matched_boundaries / reference_count if reference_count else float(predicted_count == 0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    onset_errors = []
    offset_errors = []
    timed_starts = []
    intervals = []
    for index, cue in enumerate(ref_cues):
        start = ref_starts[index]
        end = ref_starts[index + 1] if index + 1 < len(ref_starts) else len(ref_text)
        anchors = [r for r in range(start, end) if r in matches]
        coverage = len(anchors) / (end - start)
        # Endpoint errors are meaningful only if both ends have nearby text
        # anchors. Report uncovered intervals rather than inventing timings.
        if (coverage < .8 or not anchors or anchors[0] - start > 1
                or end - anchors[-1] - 1 > 1):
            intervals.append({"reference_index": index, "scored": False, "text_coverage": coverage})
            continue
        first = hyp_cues[bisect_right(hyp_starts, matches[anchors[0]]) - 1]
        last = hyp_cues[bisect_right(hyp_starts, matches[anchors[-1]]) - 1]
        onset = first.start - cue.start
        offset = last.end - cue.end
        onset_errors.append(onset)
        offset_errors.append(offset)
        timed_starts.append(cue.start)
        intervals.append({"reference_index": index, "scored": True, "text_coverage": coverage,
                          "onset_error_ms": onset * 1000, "offset_error_ms": offset * 1000})

    drift = None
    if len(timed_starts) >= 4 and timed_starts[-1] - timed_starts[0] >= 60:
        mean_time = sum(timed_starts) / len(timed_starts)
        mean_error = sum(onset_errors) / len(onset_errors)
        covariance = sum((t - mean_time) * (e - mean_error)
                         for t, e in zip(timed_starts, onset_errors))
        variance = sum((t - mean_time) ** 2 for t in timed_starts)
        drift = covariance / variance * 3_600_000  # seconds/second -> ms/hour
    return {
        "text": text_score,
        "boundaries": {"tolerance_chars": boundary_tolerance_chars, "matched": matched_boundaries,
                       "reference": reference_count, "predicted": predicted_count,
                       "precision": precision, "recall": recall, "f1": f1},
        "timing": {"reference_intervals": len(ref_cues), "scored_intervals": len(onset_errors),
                   "coverage": len(onset_errors) / len(ref_cues) if ref_cues else None,
                   "onset": _error_summary(onset_errors), "offset": _error_summary(offset_errors),
                   "onset_drift_ms_per_hour": drift, "intervals": intervals},
    }
