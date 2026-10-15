"""Long-recording ASR orchestration with durable recognition/alignment stages."""
from __future__ import annotations

from dataclasses import asdict
import math
import os
from pathlib import Path
import time
import uuid

from lubo.asr_backend import (firered_code_root, firered_model_root, firered_python_path,
                             firered_worker_path, model_identity)
from lubo.asr_profiles import get_asr_profile
from lubo.asr_timing import (finalize_cues, spoken_text, split_timed_units,
                             strip_trailing_punct, units_from_sentence)
from lubo.quality_audio import AudioWindow, plan_windows, write_window
from lubo.quality_checkpoint import (JobLock, RecordingCheckpoint, recording_fingerprint,
                                     sha256_file, source_signature)
from lubo.subtitles import Cue
from lubo.wav_audio import WavReader


def stitch_windows(windows: list[AudioWindow], samples: list[dict], duration: float,
                   max_chars: int) -> list[Cue]:
    """Assign measured words to their source core and retain their absolute times."""
    if len(windows) != len(samples):
        raise ValueError("识别结果不完整，未写出部分字幕")
    units = []
    for window, sample in zip(windows, samples):
        if sample.get("error"):
            raise RuntimeError(f"第 {window.index + 1} 块识别失败：{sample['error']}")
        words = sample.get("words", [])
        text = sample.get("text", "")
        if not spoken_text(text):
            continue
        raw_text = " ".join(word["text"] for word in words)
        if not words or spoken_text(raw_text) != spoken_text(text):
            raise ValueError(f"第 {window.index + 1} 块缺少完整的字词对齐结果")
        previous_start = -1.0
        read_duration = (window.read_end_ms - window.read_start_ms) / 1000
        for word in words:
            start, end = word["start_time"], word["end_time"]
            if not (math.isfinite(start) and math.isfinite(end)
                    and 0 <= start <= end <= read_duration + .1
                    and start >= previous_start):
                raise ValueError(f"第 {window.index + 1} 块时间戳无效，未按字数猜测时间")
            previous_start = start
        sentence = {"text": text, "raw_text": raw_text,
                    "timestamp": [[word["start_time"] * 1000, word["end_time"] * 1000]
                                  for word in words]}
        for unit_text, start, end in units_from_sentence(sentence, text):
            if window.owns_word(start, end):
                units.append((unit_text, max(0, start + window.offset_seconds),
                              min(duration, end + window.offset_seconds)))
    cues = [Cue(c.start, c.end, strip_trailing_punct(c.text))
            for c in split_timed_units(units, max(1, int(max_chars))) if strip_trailing_punct(c.text)]
    result = finalize_cues(cues, duration, max_chars)
    if spoken_text("".join(c.text for c in result)) != spoken_text("".join(c.text for c in cues)):
        raise ValueError("时间轴整理会丢失文字，请保留日志以检查异常对齐")
    return result


def _validate_native_words(sample: dict) -> None:
    """Check FireRed's native word times; callers may still use forced alignment."""
    text = sample.get("text", "")
    words = sample.get("native_words", [])
    if not spoken_text(text):
        return
    if not words:
        raise ValueError(f"FireRed 块 {sample['id']} 返回了字幕文字，但没有原生词级时间")
    normalized = []
    previous_start = -1.0
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            raise ValueError(f"FireRed 块 {sample['id']} 的原生 words 字段结构无效")
        start, end = word.get("start_time"), word.get("end_time")
        if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or not (0 <= start <= end <= sample["duration"] + .1 and start >= previous_start)):
            raise ValueError(f"FireRed 块 {sample['id']} 的原生时间戳无效")
        previous_start = start
        normalized.append(word["text"])
    if spoken_text("".join(normalized)) != spoken_text(text):
        raise ValueError(f"FireRed 块 {sample['id']} 的原生词时间没有覆盖完整识别文字")


def _build_samples(windows: list[AudioWindow], reader: WavReader,
                   checkpoint: RecordingCheckpoint, profile, language: str,
                   progress_path: Path) -> list[dict]:
    samples = []
    for window in windows:
        chunk_id = f"chunk-{window.index:05d}"
        target = checkpoint.chunk_path(chunk_id)
        expected_duration = (window.read_end_ms - window.read_start_ms) / 1000
        cached = checkpoint.verified_chunk(chunk_id)
        if cached is not None:
            try:
                from lubo.quality_worker import audio_duration
                actual_duration = audio_duration(str(cached))
                if abs(actual_duration - expected_duration) > .06:
                    cached = None
            except (OSError, ValueError):
                cached = None
        if cached is None:
            temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.part")
            try:
                write_window(reader, window, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            from lubo.quality_worker import audio_duration
            actual_duration = audio_duration(str(target))
            if abs(actual_duration - expected_duration) > .06:
                raise ValueError(f"第 {window.index + 1} 块音频长度不匹配，已停止识别")
            checkpoint.remember_chunk(chunk_id, sha256_file(target), actual_duration)
        sample = {"id": chunk_id, "audio": str(target)}
        if profile.supports_language and language:
            sample["language"] = language
        samples.append(sample)
        if len(samples) % 10 == 0 or len(samples) == len(windows):
            from lubo.quality_worker import publish_progress
            publish_progress(progress_path, .04 + .03 * len(samples) / max(1, len(windows)),
                             f"准备识别音频块：{len(samples)}/{len(windows)}")
    return samples


def _validate_source_before_commit(source: Path, expected_signature: dict,
                                   checkpoint: RecordingCheckpoint | None = None) -> None:
    if checkpoint is not None:
        checkpoint.assert_source_unchanged()
        return
    current = source_signature(source)
    if current != expected_signature:
        raise RuntimeError("识别期间源音频发生变化，已拒绝提交本次缓存")


def run_recording(job: dict, result_path: Path) -> dict:
    from lubo.quality_worker import (align_results, audio_duration, publish_progress,
                                     run_job, save_report)

    profile = get_asr_profile(job.get("model_key", "qwen3"))
    if not profile.supports_context and job.get("context"):
        raise ValueError(f"{profile.display_name} 不支持 context；实际任务必须省略此字段")
    if not profile.supports_language and job.get("language"):
        raise ValueError(f"{profile.display_name} 不支持 language；实际任务必须省略此字段")
    if not isinstance(job.get("effective_parameters"), dict):
        raise ValueError("识别任务缺少 effective_parameters，无法安全复用持久缓存")
    model_root = Path(job["model_root"]).resolve()
    expected_identity = model_identity(profile, firered_model_root())
    if expected_identity != job.get("model_identity"):
        raise RuntimeError("当前模型 revision 与任务 fingerprint 不一致，请重新启动识别")
    computed_fingerprint, identity = recording_fingerprint(
        job["source_sha256"], expected_identity, job["effective_parameters"])
    if computed_fingerprint != job.get("job_fingerprint") or identity != job.get("job_identity"):
        raise ValueError("识别任务 fingerprint 校验失败")
    source = Path(job["wav_path"])
    expected_signature = job.get("source_signature")
    if not isinstance(expected_signature, dict) or source_signature(source) != expected_signature:
        raise RuntimeError("识别音频在任务启动前已变化，已拒绝使用旧 fingerprint")
    job_root = Path(job["job_dir"]).resolve()
    if job_root.name != computed_fingerprint:
        raise ValueError("持久任务目录与 fingerprint 不匹配")
    progress_path = Path(job["progress_path"])
    log_path = Path(job["log_path"])
    attempt_report_path = log_path.with_name("recording-report.json")

    with JobLock(job_root / "job.lock"):
        checkpoint = RecordingCheckpoint(
            job_root, computed_fingerprint, identity,
            source_path=source, expected_source_signature=expected_signature,
        )
        for warning in checkpoint.cache_warnings:
            print(f"Checkpoint cache ignored: {warning}", flush=True)
        _validate_source_before_commit(source, expected_signature, checkpoint)
        publish_progress(progress_path, .01, "分析音频停顿，准备可恢复识别分块")
        windows = plan_windows(source, core_ms=24000, search_ms=4000, context_ms=1600)
        with WavReader(source) as reader:
            samples = _build_samples(windows, reader, checkpoint, profile,
                                     job.get("language", ""), progress_path)
            duration = reader.duration
        _validate_source_before_commit(source, expected_signature, checkpoint)

        if profile.requires_forced_aligner:
            from lubo.quality_models import download_model

            publish_progress(progress_path, .075, "检查时间对齐模型，首次运行可能需要下载")
            download_model("aligner", model_root)
        if profile.runtime == "application":
            from lubo.quality_models import download_model

            publish_progress(progress_path, .02, "检查本地 ASR 模型，首次运行可能需要下载")
            download_model("asr", model_root)
        else:
            if not firered_python_path().is_file():
                raise RuntimeError(f"FireRed 隔离解释器不存在：{firered_python_path()}")
            if not firered_worker_path().is_file():
                raise RuntimeError(f"FireRed worker 脚本不存在：{firered_worker_path()}")
            if not firered_code_root().is_dir() or not firered_model_root().is_dir():
                raise RuntimeError("FireRed 隔离代码或固定模型目录不存在")

        recognition: dict[str, dict] = {}
        missing = []
        for sample in samples:
            cached = checkpoint.read_stage("recognition", sample["id"])
            if cached is None or not isinstance(cached.get("text"), str):
                missing.append(sample)
                continue
            item = {"id": sample["id"], "audio": sample["audio"],
                    "duration": audio_duration(sample["audio"]), **cached}
            item["audio_sha256"] = sha256_file(Path(sample["audio"]))
            recognition[sample["id"]] = item
        if missing:
            engine_job = {
                "engine": profile.key,
                "samples": missing,
                "max_chars": job["max_chars"],
                "asr_model": str(model_root / Path(profile.asr_repo).name) if profile.runtime == "application" else "",
                "aligner_model": str(model_root / Path(profile.aligner_repo).name)
                    if profile.requires_forced_aligner else "",
                "placement": "cpu",
                "aligner_device": "cpu",
                "align": False,
                "context": job.get("context", ""),
                "language": job.get("language", ""),
                "progress_path": str(progress_path),
                "cancel_path": job.get("cancel_path"),
                "log_path": job["log_path"],
            }
            if not profile.supports_context:
                engine_job.pop("context", None)
            if not profile.supports_language:
                engine_job.pop("language", None)
            recognition_path = log_path.with_name("recognition.json")
            recognized_report = run_job(engine_job, recognition_path, checkpoint=checkpoint)
            for item in recognized_report["samples"]:
                recognition[item["id"]] = item
            if not recognized_report.get("complete") or not recognized_report.get("ok"):
                save_report(result_path, {"schema_version": 1, "complete": False, "ok": False,
                                          "error": "ASR stage incomplete", "samples": recognized_report["samples"]})
                raise RuntimeError("有音频块未能完整识别；未生成字幕，已保留成功块和日志供重试")

        if len(recognition) != len(windows):
            raise RuntimeError("识别 checkpoint 缺块；未生成字幕")
        aligned_report = {"schema_version": 1, "engine": profile.key, "complete": False,
                          "ok": False, "samples": [recognition[f"chunk-{index:05d}"]
                                                    for index in range(len(windows))],
                          "events": [], "runtime": {}}

        def alignment_log(message: str) -> None:
            print(message, flush=True)
            aligned_report["events"].append({"elapsed": time.time(), "message": message})
            save_report(attempt_report_path, aligned_report)

        if profile.requires_forced_aligner:
            align_job = {
                "aligner_model": str(model_root / Path(profile.aligner_repo).name),
                "aligner_device": "cpu",
                "max_chars": job["max_chars"],
                "progress_path": str(progress_path),
            }
            align_results(align_job, aligned_report, alignment_log, checkpoint=checkpoint)
        else:
            # Future native-only profiles must still commit and validate measured words.
            for item in aligned_report["samples"]:
                item["words"] = item.get("native_words", [])
                item["cues"] = []

        if not all(item.get("cues") is not None and "error" not in item
                   for item in aligned_report["samples"]):
            raise RuntimeError("时间对齐有失败块；未生成字幕")
        _validate_source_before_commit(source, expected_signature, checkpoint)
        cues = stitch_windows(windows, aligned_report["samples"], duration, job["max_chars"])
        _validate_source_before_commit(source, expected_signature, checkpoint)
        result = {"schema_version": 1, "complete": True, "ok": True,
                  "duration": duration, "chunks": len(windows), "model_key": profile.key,
                  "job_fingerprint": computed_fingerprint,
                  "alignment": "qwen3-forced-aligner" if profile.requires_forced_aligner else "native",
                  "cues": [asdict(cue) for cue in cues]}
        save_report(result_path, result)
        publish_progress(progress_path, 1.0, f"高精度字幕完成，共 {len(cues)} 条")
        return result
