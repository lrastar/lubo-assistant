"""Isolated local ASR evaluation worker, also usable as a frozen entrypoint.

Jobs contain audio and model settings, never evaluation reference transcripts.
The benchmark currently accepts bounded clips. Long-recording scheduling is
kept separate so an entire recording cannot accidentally exhaust memory.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
import wave

from lubo.asr_adapters import create_quality_asr_adapter
from lubo.quality_checkpoint import RecordingCheckpoint, sha256_file


def save_report(path: Path, report: dict) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(.05)


def publish_progress(path: Path, progress: float, message: str) -> None:
    progress = min(max(float(progress), 0.0), 1.0)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        previous_progress = previous.get("progress") if isinstance(previous, dict) else None
        if (isinstance(previous_progress, (int, float)) and not isinstance(previous_progress, bool)
                and math.isfinite(previous_progress)):
            progress = max(progress, min(max(float(previous_progress), 0.0), 1.0))
    except (OSError, ValueError, TypeError):
        pass
    save_report(path, {"progress": progress, "message": message})


def audio_duration(path: str) -> float:
    with wave.open(path, "rb") as stream:
        if stream.getframerate() != 16000 or stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError("Worker expects 16 kHz mono PCM16 WAV")
        duration = stream.getnframes() / stream.getframerate()
        if not 0 < duration <= 40:
            raise ValueError("Evaluation clips must be between 0 and 40 seconds")
        return duration


class RecognitionLimitError(RuntimeError):
    """The decoder did not finish; its partial output must not become subtitles."""


def recognize_with_recovery(sample: dict, decode, log, depth: int = 0) -> dict:
    try:
        return decode(sample)
    except RecognitionLimitError:
        from lubo.wav_audio import WavReader
        from lubo.quality_audio import AudioWindow, _quiet_cut, write_window

        duration = audio_duration(sample["audio"])
        if duration <= 6 or depth >= 3:
            raise RecognitionLimitError(f"短片段仍未得到完整识别结果：{sample['id']}") from None
        log(f"识别输出未正常结束，缩短片段重试：{sample['id']}")
        results = []
        with WavReader(Path(sample["audio"])) as reader, tempfile.TemporaryDirectory(prefix="lubo_retry_") as directory:
            middle = reader.total_ms // 2
            search = min(2000, reader.total_ms // 8)
            cut = _quiet_cut(reader, middle - search, middle, middle + search)
            for index, (start, end) in enumerate(((0, cut), (cut, reader.total_ms))):
                path = Path(directory) / f"part-{index}.wav"
                write_window(reader, AudioWindow(index, start, end, start, end), path)
                child = dict(sample, id=f"{sample['id']}.{index}", audio=str(path))
                results.append(recognize_with_recovery(child, decode, log, depth + 1))
        languages = {result.get("language") for result in results if result.get("language")}
        return {"text": " ".join(result["text"].strip() for result in results if result["text"].strip()),
                "language": next(iter(languages)) if len(languages) == 1 else None,
                "recovered": True, "recovery_parts": sum(result.get("recovery_parts", 1) for result in results)}


def run_job(job: dict, result_path: Path, checkpoint: RecordingCheckpoint | None = None) -> dict:
    engine = job["engine"]
    torch = None
    if engine in ("paraformer", "fun_asr_nano"):
        import torch as torch_module
        torch = torch_module
        torch.set_num_threads(max(1, min(int(job.get("cpu_threads", 8)), os.cpu_count() or 1)))
        torch.manual_seed(0)
    report = {"schema_version": 1, "engine": engine, "complete": False,
              "ok": False, "samples": [], "events": [],
              "runtime": {"torch": torch.__version__ if torch is not None else None}}
    started = time.perf_counter()

    def log(message: str) -> None:
        print(message, flush=True)
        report["events"].append({"elapsed": time.perf_counter() - started, "message": message})
        save_report(result_path, report)

    def save_item(sample: dict, item: dict, elapsed: float) -> dict:
        item.setdefault("id", sample["id"])
        item.setdefault("audio", sample["audio"])
        item.setdefault("duration", audio_duration(sample["audio"]))
        item.setdefault("audio_sha256", sha256_file(Path(sample["audio"])))
        item.setdefault("native_words", [])
        item.setdefault("confidence", None)
        item["elapsed_seconds"] = elapsed
        if "error" not in item and checkpoint is not None:
            checkpoint.commit_stage("recognition", sample["id"], {
                key: item[key] for key in ("duration", "audio_sha256", "text", "language", "native_words", "confidence",
                                          "native_alignment_valid", "native_alignment_error")
                if key in item
            })
        elif "error" in item and checkpoint is not None:
            checkpoint.record_failure("recognition", sample["id"], str(item["error"]))
        report["samples"].append(item)
        if job.get("progress_path"):
            done, total = len(report["samples"]), max(1, len(job["samples"]))
            publish_progress(Path(job["progress_path"]), .08 + .64 * done / total,
                             f"高精度识别：{done}/{total} 块")
        log(f"Processed {len(report['samples'])}/{len(job['samples'])}: {sample['id']}")
        return item

    def record_sample(sample: dict, infer) -> dict:
        start = time.perf_counter()
        item = {"id": sample["id"], "audio": sample["audio"]}
        try:
            item["duration"] = audio_duration(sample["audio"])
            item["audio_sha256"] = sha256_file(Path(sample["audio"]))
            item.update(infer(sample))
        except Exception:
            item["error"] = traceback.format_exc()
        return save_item(sample, item, time.perf_counter() - start)

    if engine == "paraformer":
        from dataclasses import asdict
        from lubo.asr import Transcriber
        from lubo.config import AsrOptions

        transcriber = Transcriber(AsrOptions(device=job.get("device", "cuda")), log=log)
        try:
            transcriber.load()

            def infer_baseline(sample):
                cues = transcriber.transcribe(sample["audio"], max_chars=job.get("max_chars", 22))
                return {"text": " ".join(c.text for c in cues), "cues": [asdict(c) for c in cues]}

            for sample in job["samples"]:
                record_sample(sample, infer_baseline)
        finally:
            transcriber.close()
    elif engine in ("qwen3", "firered_asr2_aed"):
        adapter = create_quality_asr_adapter(engine)
        adapter.load(job, log)
        try:
            if engine == "qwen3":
                report["runtime"]["torch"] = adapter.torch.__version__
                report["runtime"]["transformers"] = adapter.transformers_version
                report["model"] = adapter.model_info
                for sample in job["samples"]:
                    record_sample(sample, lambda item: recognize_with_recovery(item, adapter.transcribe, log))
            else:
                report["model_key"] = engine

                def record_external(sample: dict, result: dict) -> None:
                    started_item = time.perf_counter()
                    if "error" not in result:
                        try:
                            from lubo.quality_recording import _validate_native_words
                            _validate_native_words(result)
                            result["native_alignment_valid"] = True
                        except Exception as exc:
                            # Transcript success is independent from native timing.
                            # FireRed timing gaps fall back to the Qwen forced aligner.
                            result["native_alignment_valid"] = False
                            result["native_alignment_error"] = str(exc)
                    save_item(sample, result, float(result.get("elapsed_seconds") or
                                                     time.perf_counter() - started_item))

                adapter.transcribe_batch(job["samples"], record_external)
        finally:
            adapter.close()
        if job.get("align", True):
            align_results(job, report, log, checkpoint=checkpoint)
    elif engine == "fun_asr_nano":
        import transformers
        from transformers import FunAsrNanoForConditionalGeneration, FunAsrNanoProcessor

        checkpoint_path = Path(job["asr_model"])
        if not (checkpoint_path / "lubo-model.json").is_file():
            raise ValueError("ASR checkpoint download has not been verified")
        processor = FunAsrNanoProcessor.from_pretrained(checkpoint_path, local_files_only=True)
        model = FunAsrNanoForConditionalGeneration.from_pretrained(
            checkpoint_path, local_files_only=True, dtype=torch.float32, attn_implementation="sdpa")
        report["runtime"]["transformers"] = transformers.__version__

        def infer_nano(sample):
            inputs = processor.apply_transcription_request(
                audio=sample["audio"], language=sample.get("language", job.get("language")),
                prompt=job.get("context", ""),
                processor_kwargs={"return_tensors": "pt", "audio_kwargs": {"sampling_rate": 16000}},
            )
            inputs = inputs.to(model.device, model.dtype)
            limit = min(1024, max(256, int(audio_duration(sample["audio"]) * 16) + 64))
            with torch.inference_mode():
                outputs = model.generate(**inputs, max_new_tokens=limit, do_sample=False)
            generated = outputs[:, inputs["input_ids"].shape[1]:]
            if generated.shape[1] >= limit:
                raise RecognitionLimitError("Recognition did not finish before its token limit")
            return {"text": processor.batch_decode(generated, skip_special_tokens=True)[0].strip(),
                    "language": sample.get("language", job.get("language"))}

        for sample in job["samples"]:
            record_sample(sample, infer_nano)
        del model, processor
        gc.collect()
    else:
        raise ValueError(f"Unknown engine: {engine}")
    report.update(complete=True, ok=all("error" not in item for item in report["samples"]),
                  elapsed_seconds=time.perf_counter() - started)
    save_report(result_path, report)
    return report


def run_quick_job(job: dict, result_path: Path) -> dict:
    """Run the legacy Paraformer flow in its own cancellable worker process."""
    from dataclasses import asdict
    from lubo.asr import Transcriber
    from lubo.config import AsrOptions
    from lubo.ffmpeg import Cancelled

    report = {"schema_version": 1, "engine": "paraformer", "complete": False,
              "ok": False, "cues": [], "events": []}
    started = time.perf_counter()
    save_report(result_path, report)

    def log(message: str) -> None:
        print(message, flush=True)
        report["events"].append({"elapsed": time.perf_counter() - started, "message": message})

    cancel_event = threading.Event()
    stop_watcher = threading.Event()

    def watch_cancel_file() -> None:
        cancel_path = job.get("cancel_path")
        while not stop_watcher.wait(.1):
            if cancel_path and Path(cancel_path).exists():
                cancel_event.set()
                return

    watcher = threading.Thread(target=watch_cancel_file, name="asr-cancel-watch", daemon=True)
    watcher.start()
    options = AsrOptions(
        device=job.get("device", "cuda"),
        model=job.get("model", "paraformer-zh"),
        vad_model=job.get("vad_model", "fsmn-vad"),
        punc_model=job.get("punc_model", "ct-punc"),
        chunk_seconds=job.get("chunk_seconds", 120.0),
        engine="paraformer",
    )
    transcriber = Transcriber(options, log=log)
    try:
        publish_progress(Path(job["progress_path"]), 0.0, "加载快捷识别模型")
        cues = transcriber.transcribe(
            Path(job["wav_path"]), max_chars=job.get("max_chars", 22),
            on_progress=lambda value: publish_progress(
                Path(job["progress_path"]), value, f"快捷识别：{value:.0%}"),
            cancel=cancel_event,
        )
        if cancel_event.is_set():
            raise Cancelled()
        report.update(complete=True, ok=True, cues=[asdict(cue) for cue in cues],
                      elapsed_seconds=time.perf_counter() - started)
        save_report(result_path, report)
        return report
    finally:
        stop_watcher.set()
        watcher.join(timeout=1)
        transcriber.close()


def align_results(job: dict, report: dict, log,
                  checkpoint: RecordingCheckpoint | None = None) -> None:
    from dataclasses import asdict
    from lubo.asr_timing import finalize_cues, sentence_to_cues, spoken_text

    pending = []
    for item in report["samples"]:
        if "error" in item:
            continue
        if checkpoint is not None:
            cached = checkpoint.read_stage("alignment", item["id"])
            if cached is not None:
                item.update(cached)
                continue
        if not item.get("text", "").strip():
            aligned = {"words": [], "cues": [], "alignment": "qwen3-forced-aligner"}
            item.update(aligned)
            if checkpoint is not None:
                checkpoint.commit_stage("alignment", item["id"], aligned)
            continue
        pending.append(item)
    if not pending:
        if job.get("progress_path"):
            publish_progress(Path(job["progress_path"]), .98, "时间对齐缓存已完整")
        return

    import torch
    from transformers import Qwen3ASRForTokenClassification, Qwen3ASRProcessor

    checkpoint_path = Path(job["aligner_model"])
    if not (checkpoint_path / "lubo-model.json").is_file():
        raise ValueError("Alignment checkpoint download has not been verified")
    device = job.get("aligner_device", "cuda" if torch.cuda.is_available() else "cpu")
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    log(f"Loading forced aligner: {checkpoint_path.name}, device={device}")
    if job.get("progress_path"):
        publish_progress(Path(job["progress_path"]), .74, "加载时间对齐模型")
    processor = Qwen3ASRProcessor.from_pretrained(checkpoint_path, local_files_only=True)
    model = Qwen3ASRForTokenClassification.from_pretrained(
        checkpoint_path, local_files_only=True, dtype=dtype, attn_implementation="sdpa")
    model.to(device)
    for index, item in enumerate(pending, 1):
        if job.get("progress_path"):
            publish_progress(Path(job["progress_path"]), .75 + .23 * index / len(pending),
                             f"逐字时间对齐：{index}/{len(pending)} 块")
        try:
            inputs, word_lists = processor.prepare_forced_aligner_inputs(
                audio=item["audio"], transcript=item["text"], language=None)
            inputs = inputs.to(device, dtype)
            with torch.inference_mode():
                output = model(**inputs)
            words = processor.decode_forced_alignment(
                logits=output.logits, input_ids=inputs["input_ids"], word_lists=word_lists,
                timestamp_token_id=model.config.timestamp_token_id)[0]
            raw_text = " ".join(word["text"] for word in words)
            if not words or spoken_text(raw_text) != spoken_text(item["text"]):
                raise ValueError("Alignment did not preserve the recognized text")
            sentence = {"text": item["text"], "raw_text": raw_text,
                        "timestamp": [[word["start_time"] * 1000, word["end_time"] * 1000] for word in words],
                        "start": words[0]["start_time"] * 1000, "end": words[-1]["end_time"] * 1000}
            cues = finalize_cues(sentence_to_cues(sentence, job.get("max_chars", 22)), item["duration"])
            aligned = {"words": words, "cues": [asdict(cue) for cue in cues],
                       "alignment": "qwen3-forced-aligner"}
            item.update(aligned)
            if checkpoint is not None:
                checkpoint.commit_stage("alignment", item["id"], aligned)
        except Exception:
            item["error"] = traceback.format_exc()
            if checkpoint is not None:
                checkpoint.record_failure("alignment", item["id"], item["error"])
        log(f"Aligned {item['id']}")
    del model, processor
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    log("Alignment model released")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    args.result.parent.mkdir(parents=True, exist_ok=True)
    from lubo.asr_backend import runtime_home_directory

    support = runtime_home_directory() / "quality_support"
    if support.is_dir():
        sys.path.insert(0, str(support))
    try:
        job = json.loads(args.job.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}
    if not isinstance(job, dict):
        job = {}
    log_path = Path(job.get("log_path", args.result.with_suffix(".log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(log_path, "w", encoding="utf-8", buffering=1)
    try:
        if not job:
            job = json.loads(args.job.read_text(encoding="utf-8"))
        if job.get("mode") == "recording":
            from lubo.quality_recording import run_recording
            result = run_recording(job, args.result)
        elif job.get("mode") == "quick":
            result = run_quick_job(job, args.result)
        elif not job.get("samples"):
            raise ValueError("No benchmark samples")
        else:
            result = run_job(job, args.result)
    except BaseException:
        # Preserve partial samples for audit if setup or a later stage fails.
        try:
            result = json.loads(args.result.read_text(encoding="utf-8")) if args.result.exists() else {}
        except (OSError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {"partial_report": result}
        result.update(ok=False, complete=False, error=traceback.format_exc())
        save_report(args.result, result)
    sys.stdout.flush()
    # Frozen workers must not proceed into the GUI or persist CUDA resources.
    if getattr(sys, "frozen", False):
        os._exit(0 if result["ok"] else 1)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
