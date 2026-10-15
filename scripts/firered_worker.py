"""Run pinned FireRedASR2-AED over one JSON job and write incremental results."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from firered_runtime import (
    CODE_REVISION,
    MODEL_NAME,
    MODEL_REVISION,
    check_code_revision,
    read_marker,
    resolve_code_dir,
    resolve_model_dir,
    sha256_file,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows readers can briefly deny replacement even after a complete JSON
    # was written. Keep the previous report intact and retry only these sharing
    # errors; persistent permissions and other IO failures still fail the job.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(.05)


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def peak_working_set_bytes() -> int | None:
    if os.name != "nt":
        return None
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    # ctypes defaults to a 32-bit int return type. Explicitly retain the
    # pseudo-handle width on 64-bit Windows before passing it to PSAPI.
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
    return int(counters.PeakWorkingSetSize) if ok else None


def install_unicode_sentencepiece_loader() -> None:
    """Load SentencePiece model bytes through Python's Unicode-safe file IO.

    The pinned upstream tokenizer calls SentencePiece's native ``Load(path)``
    API, whose Windows file open fails for paths containing non-ASCII chars.
    Keep the upstream checkout immutable and adapt only this worker process.
    """
    import sentencepiece as spm

    processor = spm.SentencePieceProcessor
    native_load = processor.Load
    if getattr(native_load, "_lubo_unicode_safe", False):
        return

    def unicode_safe_load(self: Any, model_file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(model_file, (str, os.PathLike)):
            try:
                serialized_model = Path(model_file).read_bytes()
            except (OSError, TypeError, ValueError):
                # Preserve upstream behavior and diagnostics for missing or
                # non-path inputs; the model's file integrity is checked first.
                return native_load(self, model_file, *args, **kwargs)
            return self.LoadFromSerializedProto(serialized_model)
        return native_load(self, model_file, *args, **kwargs)

    unicode_safe_load._lubo_unicode_safe = True  # type: ignore[attr-defined]
    processor.Load = unicode_safe_load


def load_job(path: Path) -> tuple[list[dict[str, str]], Path, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("samples")
    report_value = payload.get("report_path")
    if not isinstance(samples, list) or not samples:
        raise ValueError("job.samples must be a non-empty list")
    if not isinstance(report_value, str) or not report_value:
        raise ValueError("job.report_path must be a non-empty path")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(samples):
        if not isinstance(raw, dict):
            raise ValueError(f"job.samples[{index}] must be an object")
        sample_id = str(raw.get("id", "")).strip()
        audio_value = raw.get("audio")
        if not sample_id or not isinstance(audio_value, str) or not audio_value:
            raise ValueError(f"job.samples[{index}] requires non-empty id and audio")
        if sample_id in seen_ids:
            raise ValueError(f"job has duplicate sample id: {sample_id}")
        seen_ids.add(sample_id)
        audio_path = Path(audio_value).expanduser()
        if not audio_path.is_absolute():
            audio_path = path.parent / audio_path
        normalized.append({"id": sample_id, "audio": str(audio_path.resolve())})
    report_path = Path(report_value).expanduser()
    if not report_path.is_absolute():
        report_path = path.parent / report_path
    return normalized, report_path.resolve(), payload


def validate_audio(path: Path, soundfile: Any) -> float:
    if not path.is_file():
        raise FileNotFoundError(path)
    info = soundfile.info(str(path))
    if info.samplerate != 16000 or info.channels != 1 or info.subtype != "PCM_16":
        raise ValueError(
            f"FireRed requires 16 kHz mono PCM_16 WAV; got {info.samplerate} Hz, "
            f"{info.channels} channel(s), {info.subtype}: {path}"
        )
    duration_s = float(info.frames / info.samplerate)
    if duration_s > 60.0:
        raise ValueError(f"FireRedASR2-AED input must be <= 60 seconds; got {duration_s:.3f}: {path}")
    return duration_s


def normalize_timestamp(value: Any, duration_s: float) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    words: list[dict[str, Any]] = []
    last_start = -1.0
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return None
        text, start, end = item
        try:
            start_s, end_s = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if not isinstance(text, str) or not text or not math.isfinite(start_s) or not math.isfinite(end_s):
            return None
        if start_s < 0 or end_s < start_s or end_s > duration_s + 0.1 or start_s < last_start:
            return None
        words.append({"text": text, "start_time": start_s, "end_time": end_s})
        last_start = start_s
    return words if words else None


def run_job(job_path: Path, model_dir_override: str | None = None) -> int:
    job_started = time.perf_counter()
    samples, report_path, job = load_job(job_path)
    if job.get("device", "cpu") != "cpu":
        raise ValueError("This validated FireRed worker currently supports device='cpu' only")
    if job.get("precision", "float32") not in ("float32", "fp32"):
        raise ValueError("This validated FireRed worker currently supports float32 precision only")
    if int(job.get("batch_size", 1)) != 1:
        raise ValueError("This worker requires batch_size=1 so each sample is committed immediately")
    verification_started = time.perf_counter()
    model_dir_value = model_dir_override or job.get("model_dir")
    code_dir_value = job.get("code_dir")
    model_dir = resolve_model_dir(model_dir_value)
    code_dir = resolve_code_dir(code_dir_value)
    check_code_revision(code_dir)
    marker = read_marker(model_dir)
    verification_elapsed_s = round(time.perf_counter() - verification_started, 3)
    if marker.get("code_revision") != CODE_REVISION:
        raise RuntimeError("FireRed code revision differs from the verified model marker")
    asr_source = code_dir / "fireredasr2s"
    if not (asr_source / "fireredasr2" / "asr.py").is_file():
        raise RuntimeError(
            f"FireRed code is missing: {code_dir}; run scripts/setup_firered.ps1"
        )
    sys.path.insert(0, str(code_dir))

    import soundfile as sf
    import torch

    install_unicode_sentencepiece_loader()

    from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config

    thread_count = max(1, int(os.environ.get("LUBO_FIRERED_TORCH_THREADS", min(8, os.cpu_count() or 4))))
    torch.set_num_threads(thread_count)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    if torch.cuda.is_available():
        # This worker intentionally remains on CPU until a separate GPU profile is validated.
        pass

    config = FireRedAsr2Config(
        use_gpu=False,
        use_half=False,
        beam_size=3,
        nbest=1,
        decode_max_len=0,
        softmax_smoothing=1.25,
        aed_length_penalty=0.6,
        eos_penalty=1.0,
        return_timestamp=True,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "engine": "firered_asr2",
        "model": MODEL_NAME,
        "repo_id": marker["repo_id"],
        "model_revision": MODEL_REVISION,
        "code_revision": CODE_REVISION,
        "model_sha256": marker["files"]["model.pth.tar"]["sha256"],
        "model_dir": str(model_dir),
        "code_dir": str(code_dir),
        "device": "cpu",
        "precision": "float32",
        "torch_threads": thread_count,
        "complete": False,
        "ok": False,
        "status": "running",
        "started_at_utc": utc_now(),
        "finished_at_utc": None,
        "verification_elapsed_s": verification_elapsed_s,
        "model_load_elapsed_s": None,
        "total_elapsed_s": None,
        "sample_count": len(samples),
        "completed_count": 0,
        "peak_working_set_bytes": None,
        "samples": [],
    }
    write_json_atomic(report_path, report)
    emit({"event": "start", "model": MODEL_NAME, "total": len(samples)})
    model_load_started = time.perf_counter()
    try:
        model = FireRedAsr2.from_pretrained("aed", str(model_dir), config)
        report["model_load_elapsed_s"] = round(time.perf_counter() - model_load_started, 3)
    except Exception as exc:
        report.update({"status": "failed", "error": f"model_load: {type(exc).__name__}: {exc}"})
        report["finished_at_utc"] = utc_now()
        report["model_load_elapsed_s"] = round(time.perf_counter() - model_load_started, 3)
        report["total_elapsed_s"] = round(time.perf_counter() - job_started, 3)
        report["peak_working_set_bytes"] = peak_working_set_bytes()
        write_json_atomic(report_path, report)
        emit({"event": "failed", "completed": 0, "total": len(samples), "error": report["error"]})
        return 1

    native_timestamp_method = model._get_and_fix_timestamp

    def do_not_synthesize_timestamps(hyp: dict[str, Any], hyp_ids: Any, duration_s: float):
        # Upstream currently creates evenly-spaced pseudo-times when CTC forced
        # alignment returned None. Preserve the transcript but expose no times.
        if hyp.get("timestamp") is None:
            return None
        return native_timestamp_method(hyp, hyp_ids, duration_s)

    model._get_and_fix_timestamp = do_not_synthesize_timestamps

    completed = 0
    failed = 0
    try:
        for sample in samples:
            sample_started = time.perf_counter()
            audio_path = Path(sample["audio"])
            item: dict[str, Any] = {
                "id": sample["id"],
                "audio": str(audio_path),
                "audio_sha256": None,
                "ok": False,
                "asr_ok": False,
                "alignment_ok": False,
                "text": "",
                "words": [],
                "confidence": None,
                "duration_s": None,
                "elapsed_s": None,
                "duration": None,
                "elapsed_seconds": None,
                "alignment_source": None,
            }
            try:
                duration_s = validate_audio(audio_path, sf)
                item["audio_sha256"] = sha256_file(audio_path)
                item["duration_s"] = duration_s
                item["duration"] = duration_s
                results = model.transcribe([sample["id"]], [str(audio_path)])
                if not results:
                    raise RuntimeError("FireRed returned no result")
                result = results[0]
                if "text" not in result:
                    raise RuntimeError("FireRed returned no transcript field")
                item["text"] = str(result["text"])
                item["confidence"] = result.get("confidence")
                item["ok"] = True
                item["asr_ok"] = True
                words = normalize_timestamp(result.get("timestamp"), duration_s)
                if words is not None:
                    item["words"] = words
                    item["alignment_ok"] = True
                    item["alignment_source"] = "firered_ctc"
                else:
                    item["alignment_error"] = (
                        "FireRed did not return a valid native CTC alignment; "
                        "upstream synthetic evenly-spaced timestamps were disabled."
                    )
            except Exception as exc:
                failed += 1
                item["error"] = f"{type(exc).__name__}: {exc}"
            item["elapsed_s"] = round(time.perf_counter() - sample_started, 3)
            item["elapsed_seconds"] = item["elapsed_s"]
            report["samples"].append(item)
            completed += 1
            report["completed_count"] = completed
            report["peak_working_set_bytes"] = peak_working_set_bytes()
            write_json_atomic(report_path, report)
            emit(
                {
                    "event": "progress",
                    "completed": completed,
                    "total": len(samples),
                    "sample_id": sample["id"],
                    "asr_ok": item["asr_ok"],
                    "alignment_ok": item["alignment_ok"],
                }
            )
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        report["finished_at_utc"] = utc_now()
        report["total_elapsed_s"] = round(time.perf_counter() - job_started, 3)
        report["peak_working_set_bytes"] = peak_working_set_bytes()
        write_json_atomic(report_path, report)
        emit({"event": "cancelled", "completed": completed, "total": len(samples)})
        return 130

    report["status"] = "completed" if failed == 0 else "partial"
    report["complete"] = True
    report["ok"] = failed == 0
    report["finished_at_utc"] = utc_now()
    report["total_elapsed_s"] = round(time.perf_counter() - job_started, 3)
    report["peak_working_set_bytes"] = peak_working_set_bytes()
    write_json_atomic(report_path, report)
    emit(
        {
            "event": "done",
            "status": report["status"],
            "completed": completed,
            "total": len(samples),
            "asr_failures": failed,
        }
    )
    return 0 if failed == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path, help="JSON with samples and report_path")
    parser.add_argument("--model-dir", help="Override LUBO_FIRERED_MODEL_DIR")
    args = parser.parse_args()
    return run_job(args.job.expanduser().resolve(), args.model_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FireRed worker failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
