"""Exercise the real parent/worker flow against a selected packaged EXE."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lubo.asr_backend import QualityTranscriber
from lubo.config import AsrOptions
from lubo.ffmpeg import Cancelled

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--wav", type=Path, required=True)
parser.add_argument("--result", type=Path, required=True)
parser.add_argument("--model", choices=("qwen3", "firered_asr2_aed"), default="qwen3",
                    help="Quality model profile to exercise in the packaged worker")
parser.add_argument("--cancel", action="store_true")
parser.add_argument("--cancel-at", type=float, default=.1,
                    help="Request cancellation after this progress fraction (default: .1)")
args = parser.parse_args()
if not 0 < args.cancel_at < 1:
    parser.error("--cancel-at must be greater than 0 and less than 1")
args.result.parent.mkdir(parents=True, exist_ok=True)
os.environ["LUBO_QUALITY_MODEL_DIR"] = str(args.exe.parent / "quality_models")
os.environ["LUBO_QUALITY_JOB_DIR"] = str(args.result.parent / (args.result.stem + ".quality_jobs"))
os.environ["LUBO_ASR_RUNTIMES"] = str(args.exe.parent / "asr_runtimes")
os.environ["LUBO_FIRERED_MODEL_DIR"] = str(args.exe.parent / "asr_runtimes" / "fireredasr2-aed")
progress = []
logs = []
cancel = threading.Event()
transcriber = QualityTranscriber(AsrOptions(quality_model=args.model), logs.append)
transcriber._command = lambda job, result: [str(args.exe), "--quality-worker", "--job", str(job), "--result", str(result)]
cancel_at = None
worker_pid = None

def on_progress(value):
    global cancel_at, worker_pid
    progress.append(value)
    worker_pid = transcriber._process.pid if transcriber._process is not None else worker_pid
    if args.cancel and value >= args.cancel_at and cancel_at is None:
        cancel_at = time.monotonic()
        cancel.set()

try:
    cues = transcriber.transcribe(args.wav, 22, on_progress, cancel)
    assert not args.cancel, "Cancellation was ignored"
    assert progress and progress[-1] == 1 and progress == sorted(progress)
    result = {"ok": True, "model": args.model, "cues": [asdict(cue) for cue in cues],
              "progress_count": len(progress), "logs": logs}
except Cancelled:
    assert args.cancel and cancel_at is not None
    result = {"ok": True, "model": args.model, "cancelled": True,
              "cancel_latency": time.monotonic() - cancel_at,
              "worker_pid": worker_pid, "worker_reaped": transcriber._process is None}
finally:
    transcriber.close()
args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k:v for k,v in result.items() if k not in ("cues", "logs")}, ensure_ascii=False))
