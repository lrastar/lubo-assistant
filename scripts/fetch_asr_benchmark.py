"""Fetch a fixed, evenly spaced WenetSpeech test subset before running models.

The reference text is dataset-provided; no model output is used to select rows.
Audio is downloaded for local evaluation only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request


DATASET = "TwinkStart/WenetSpeech"


def download(url: str) -> bytes:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=40) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if attempt == 5:
                raise
            if error.code == 429:
                try:
                    delay = max(60.0, float(error.headers.get("Retry-After", 60)))
                except ValueError:
                    delay = 120.0
                print(f"Dataset service rate limit; waiting {delay:.0f} seconds before retry", flush=True)
                time.sleep(delay)
            else:
                time.sleep(min(2 ** attempt, 8))
        except OSError:
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("Unreachable")


def get_rows(split: str, offset: int, length: int, cache: Path | None = None) -> dict:
    cache_file = cache / f"{split}-{offset}-{length}.json" if cache else None
    if cache_file and cache_file.exists() and time.time() - cache_file.stat().st_mtime < 900:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    query = urllib.parse.urlencode({"dataset": DATASET, "config": "default", "split": split,
                                    "offset": offset, "length": length})
    data = json.loads(download("https://datasets-server.huggingface.co/rows?" + query))
    if cache_file:
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def acquire(entry: tuple[str, dict], root: Path, ffmpeg: Path) -> dict:
    split, row_info = entry
    row = row_info["row"]
    sample_id = f"{split}-{row_info['row_idx']:05d}"
    audio = root / "audio" / (sample_id + ".wav")
    compressed = audio.with_suffix(".opus")
    source = row["audio"][0]["src"]
    if not compressed.exists():
        compressed.write_bytes(download(source))
    if not audio.exists():
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(compressed),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)], check=True)
    return {"id": sample_id, "audio": "audio/" + audio.name, "reference_text": row["text"],
            "reference_origin": "dataset", "reference_cues": None,
            "source": {"dataset": DATASET, "split": split, "row": row_info["row_idx"], "sid": row["sid"]},
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--groups", type=int, default=8, help="Evenly spaced groups of four per split")
    args = parser.parse_args()
    if args.groups < 2:
        parser.error("--groups must be at least 2")
    root = args.root.resolve()
    (root / "audio").mkdir(parents=True, exist_ok=True)
    cache = root / "row-cache"
    cache.mkdir(exist_ok=True)
    entries = []
    totals = {}
    for split in ("test_net", "test_meeting"):
        first = get_rows(split, 0, 1, cache)
        total = first["num_rows_total"]
        totals[split] = total
        # Four consecutive utterances at each of eight evenly spaced positions.
        # Fixed before recognition; use both spontaneous web and meeting speech.
        offsets = [i * (total - 4) // (args.groups - 1) for i in range(args.groups)]
        for offset in offsets:
            batch = get_rows(split, offset, 4, cache)
            if batch.get("partial") or len(batch["rows"]) != 4:
                raise RuntimeError("Dataset server returned an incomplete reference batch")
            entries.extend((split, item) for item in batch["rows"])
        print(f"Selected {split}: offsets {offsets} from {total} rows", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        samples = list(pool.map(lambda entry: acquire(entry, root, args.ffmpeg), entries))
    manifest = {"schema_version": 1, "name": f"wenetspeech-local-{len(samples)}-v1",
                "selection": f"{args.groups} evenly spaced groups of 4, per split; selected before model inference",
                "source_rows": totals, "samples": samples,
                "limitations": "Transcript benchmark only; no manual subtitle timing or Jianying baseline."}
    destination = root / "manifest.json"
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(samples)} reference/audio pairs: {destination}", flush=True)


if __name__ == "__main__":
    main()
