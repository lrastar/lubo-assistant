"""Isolated recognizer adapters selected through the central model profiles."""
from __future__ import annotations

import gc
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

from lubo.asr_profiles import get_asr_profile
from lubo.quality_checkpoint import sha256_file


class QualityAsrAdapter:
    """Small worker-side seam: load once, decode samples, release model state."""

    key: str

    def load(self, job: dict, log) -> None:
        raise NotImplementedError

    def transcribe(self, sample: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class Qwen3Adapter(QualityAsrAdapter):
    key = "qwen3"

    def __init__(self):
        self.job = None
        self.log = None
        self.torch = None
        self.model = None
        self.processor = None

    def load(self, job: dict, log) -> None:
        import torch
        import transformers
        from transformers import Qwen3ASRForConditionalGeneration, Qwen3ASRProcessor

        self.job = job
        self.log = log
        self.torch = torch
        torch.set_num_threads(max(1, min(int(job.get("cpu_threads", 8)), os.cpu_count() or 1)))
        torch.manual_seed(0)
        checkpoint = Path(job["asr_model"])
        if not (checkpoint / "lubo-model.json").is_file():
            raise ValueError("Qwen3-ASR checkpoint has not been verified")
        placement = job.get("placement", "cpu")
        kwargs = {"local_files_only": True, "dtype": torch.float32, "attn_implementation": "sdpa"}
        if placement == "audio_cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA requested but unavailable")
            kwargs["device_map"] = {
                "model.audio_tower": "cuda:0", "model.multi_modal_projector": "cuda:0",
                "model.language_model": "cpu", "lm_head": "cpu",
            }
        elif placement == "cuda":
            kwargs.update(dtype=torch.bfloat16, device_map="cuda:0")
        elif placement != "cpu":
            raise ValueError(f"Unsupported model placement: {placement}")
        log(f"Loading ASR: {checkpoint.name}, placement={placement}")
        if job.get("progress_path"):
            from lubo.quality_worker import publish_progress
            publish_progress(Path(job["progress_path"]), .06, "加载本地高精度识别模型")
        self.processor = Qwen3ASRProcessor.from_pretrained(checkpoint, local_files_only=True)
        self.model = Qwen3ASRForConditionalGeneration.from_pretrained(checkpoint, **kwargs)
        self.model_info = json.loads((checkpoint / "lubo-model.json").read_text(encoding="utf-8"))
        self.transformers_version = transformers.__version__

    def transcribe(self, sample: dict) -> dict:
        from lubo.quality_worker import RecognitionLimitError, audio_duration

        job = self.job
        processor, model, torch = self.processor, self.model, self.torch
        # Settings use '' for automatic detection; the processor requires None.
        language = sample.get("language", job.get("language")) or None
        inputs = processor.apply_transcription_request(
            audio=sample["audio"], language=language,
            prompt=job.get("context", ""),
        )
        inputs = inputs.to(model.device, model.dtype)
        token_limit = min(1024, max(256, int(audio_duration(sample["audio"]) * 16) + 64))
        with torch.inference_mode():
            outputs = model.generate(**inputs, max_new_tokens=token_limit, do_sample=False)
        generated = outputs[:, inputs["input_ids"].shape[1]:]
        if generated.shape[1] >= token_limit:
            raise RecognitionLimitError("Recognition did not finish before its token limit")
        parsed = processor.decode(generated, return_format="parsed")[0]
        return {"text": parsed["transcription"], "language": parsed.get("language")}

    def close(self) -> None:
        torch = self.torch
        self.model = None
        self.processor = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()


class FireRedAsr2Adapter(QualityAsrAdapter):
    """Batch bridge to the pinned FireRed worker in its independent Python env."""

    key = "firered_asr2_aed"

    def __init__(self):
        self.job = None
        self.log = None

    def load(self, job: dict, log) -> None:
        from lubo.asr_backend import (firered_code_root, firered_model_root,
                                     firered_python_path, firered_worker_path)

        python = firered_python_path()
        worker = firered_worker_path()
        model_root = firered_model_root()
        code_root = firered_code_root()
        if not python.is_file():
            raise RuntimeError(f"FireRed 隔离解释器不存在：{python}；可设置 LUBO_FIRERED_PYTHON")
        if not worker.is_file():
            raise RuntimeError(f"FireRed worker 脚本不存在：{worker}；可设置 LUBO_FIRERED_WORKER")
        if not model_root.is_dir() or not code_root.is_dir():
            raise RuntimeError(f"FireRed 代码或模型目录缺失：{code_root} / {model_root}")
        if job.get("context"):
            raise ValueError("FireRedASR2-AED 不支持专属词汇提示；请从实际识别任务中移除 context")
        if job.get("language"):
            raise ValueError("FireRedASR2-AED 不支持指定识别语言；请从实际识别任务中移除 language")
        self.job = job
        self.python = python
        self.worker = worker
        self.model_root = model_root
        self.code_root = code_root
        self.log = log

    def transcribe(self, sample: dict) -> dict:
        raise RuntimeError("FireRedASR2-AED uses one batch process per recording, not one process per chunk")

    def transcribe_batch(self, samples: list[dict], on_result) -> None:
        from lubo.asr_backend import _terminate_process_tree
        from lubo.asr_profiles import get_asr_profile
        from lubo.quality_worker import audio_duration, publish_progress
        from lubo.ffmpeg import Cancelled

        profile = get_asr_profile(self.key)
        # A worker may be cancelled before it writes its new report. Keep every
        # invocation's protocol files separate so a previous run cannot be
        # mistaken for this run's incremental or final report.
        run_dir = Path(self.job["log_path"]).parent / "firered-runs" / uuid.uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=False)
        request_path = run_dir / "job.json"
        report_path = run_dir / "report.json"
        stderr_path = run_dir / "stderr.log"
        request = {
            "model_dir": str(self.model_root),
            "code_dir": str(self.code_root),
            "report_path": str(report_path),
            "device": "cpu",
            "precision": "float32",
            "batch_size": 1,
            "samples": [{"id": sample["id"], "audio": sample["audio"]} for sample in samples],
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        environment = dict(os.environ, PYTHONIOENCODING="utf-8",
                           LUBO_FIRERED_MODEL_DIR=str(self.model_root),
                           LUBO_FIRERED_CODE_DIR=str(self.code_root))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        command = [str(self.python), str(self.worker), "--job", str(request_path)]
        if os.name == "nt":
            process_args = {"creationflags": creationflags}
        else:
            process_args = {"creationflags": creationflags, "start_new_session": True}
        self.log(f"FireRedASR2-AED：CPU FP32，batch=1，处理 {len(samples)} 个待识别块")
        pending = {sample["id"]: sample for sample in samples}
        consumed: set[str] = set()
        started = time.perf_counter()
        with stderr_path.open("w", encoding="utf-8", buffering=1) as stderr:
            process = subprocess.Popen(command, cwd=self.worker.parent.parent, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=stderr, **process_args)
            self.process = process
            try:
                while process.poll() is None:
                    if self.job.get("cancel_path") and Path(self.job["cancel_path"]).exists():
                        _terminate_process_tree(process)
                        raise Cancelled()
                    self._consume_report(report_path, pending, consumed, on_result)
                    if self.job.get("progress_path"):
                        done = len(consumed)
                        publish_progress(Path(self.job["progress_path"]), .08 + .64 * done / max(1, len(samples)),
                                         f"FireRed 识别：{done}/{len(samples)} 块")
                    time.sleep(.2)
                self._consume_report(report_path, pending, consumed, on_result)
                if process.returncode != 0:
                    raise RuntimeError(
                        f"FireRed worker 异常退出（{process.returncode}），日志：{stderr_path}"
                    )
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"FireRed worker 报告无效：{report_path}") from exc
                self._validate_final_report(report, profile, pending)
                if consumed != set(pending):
                    missing = sorted(set(pending) - consumed)
                    raise RuntimeError(f"FireRed worker 未返回全部识别块：{missing}；日志：{stderr_path}")
            finally:
                if process.poll() is None:
                    _terminate_process_tree(process)
                self.process = None
        self.log(f"FireRed ASR进程完成，用时 {time.perf_counter() - started:.1f}s")

    def _consume_report(self, report_path: Path, pending: dict[str, dict], consumed: set[str], on_result) -> None:
        from lubo.quality_worker import audio_duration

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(report, dict) or not isinstance(report.get("samples"), list):
            raise RuntimeError(f"FireRed worker 报告结构无效：{report_path}")
        profile = get_asr_profile(self.key)
        self._validate_report_revision(report, profile)
        for item in report["samples"]:
            if not isinstance(item, dict):
                continue
            sample_id = item.get("id")
            if sample_id not in pending or sample_id in consumed:
                continue
            source = pending[sample_id]
            expected_audio = Path(source["audio"]).expanduser().resolve()
            reported_audio = item.get("audio")
            try:
                same_audio = (isinstance(reported_audio, str)
                              and Path(reported_audio).expanduser().resolve() == expected_audio)
            except (OSError, ValueError):
                same_audio = False
            if not same_audio:
                raise RuntimeError(f"FireRed sample {sample_id} 报告的音频路径与输入不一致")
            expected_sha256 = sha256_file(expected_audio)
            if item.get("audio_sha256") != expected_sha256:
                raise RuntimeError(f"FireRed sample {sample_id} 的音频 SHA-256 与输入不一致")
            expected_duration = audio_duration(str(expected_audio))
            duration = item.get("duration_s", item.get("duration"))
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or abs(duration - expected_duration) > 0.001):
                raise RuntimeError(f"FireRed sample {sample_id} 报告的音频时长与输入不一致")
            normalized = {
                "id": sample_id,
                "audio": source["audio"],
                "audio_sha256": expected_sha256,
                "duration": expected_duration,
                "text": item.get("text", ""),
                "language": None,
                "native_words": item.get("words", []),
                "confidence": item.get("confidence"),
                "native_alignment_valid": item.get("alignment_ok"),
                "elapsed_seconds": item.get("elapsed_s", item.get("elapsed_seconds", 0.0)),
            }
            asr_ok = item.get("ok") is True or item.get("asr_ok") is True
            if not asr_ok:
                normalized["error"] = item.get("error") or "FireRed 识别失败"
            elif not isinstance(normalized["text"], str):
                normalized["error"] = "FireRed 返回的 text 不是字符串"
            elif not isinstance(normalized["native_words"], list):
                normalized["error"] = "FireRed 返回的 words 不是列表"
            on_result(source, normalized)
            consumed.add(sample_id)

    @staticmethod
    def _validate_report_revision(report: dict, profile) -> None:
        if (report.get("model_revision") != profile.asr_revision
                or report.get("code_revision") != profile.code_revision):
            raise RuntimeError("FireRed worker 实际 revision 与已指纹固定版本不一致")

    @classmethod
    def _validate_final_report(cls, report: dict, profile, pending: dict[str, dict]) -> None:
        if not isinstance(report, dict):
            raise RuntimeError("FireRed worker 报告顶层无效")
        cls._validate_report_revision(report, profile)
        expected_count = len(pending)
        samples = report.get("samples")
        sample_ids = [item.get("id") for item in samples if isinstance(item, dict)] if isinstance(samples, list) else []
        if (report.get("status") != "completed" or report.get("complete") is not True
                or report.get("ok") is not True
                or report.get("completed_count") != expected_count
                or report.get("sample_count") != expected_count
                or len(sample_ids) != expected_count or set(sample_ids) != set(pending)
                or any(not (item.get("ok") is True or item.get("asr_ok") is True)
                       for item in samples if isinstance(item, dict))):
            raise RuntimeError("FireRed worker 终态报告未证明所有输入块完整成功")


def create_quality_asr_adapter(key: str) -> QualityAsrAdapter:
    profile = get_asr_profile(key)
    adapters = {
        "qwen3": Qwen3Adapter,
        "firered_asr2_aed": FireRedAsr2Adapter,
    }
    try:
        return adapters[profile.key]()
    except KeyError as exc:
        raise ValueError(f"没有为模型 {profile.key!r} 注册 ASR adapter") from exc
