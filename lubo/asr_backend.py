"""Backend selection and the cancellable local high-quality worker boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from lubo.asr_profiles import AsrModelProfile, get_asr_profile
from lubo.ffmpeg import Cancelled
from lubo.quality_checkpoint import (atomic_write_json, recording_fingerprint,
                                    sha256_file, source_signature)
from lubo.subtitles import Cue


def create_transcriber(options, log=None):
    if options.engine == "quality":
        return QualityTranscriber(options, log)
    if options.engine == "paraformer":
        return QualityTranscriber(options, log, engine="paraformer")
    raise ValueError(f"未知识别方案：{options.engine}")


def application_directory() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def runtime_home_directory() -> Path:
    if getattr(sys, "frozen", False):
        return application_directory()
    project_root = application_directory()
    installed_root = project_root.parent.parent
    return installed_root if (installed_root / "asr_runtimes").is_dir() else project_root


def firered_runtime_root() -> Path:
    configured = os.environ.get("LUBO_ASR_RUNTIMES")
    if configured:
        return Path(configured).expanduser().resolve()
    return runtime_home_directory() / "asr_runtimes"


def quality_job_root() -> Path:
    configured = os.environ.get("LUBO_QUALITY_JOB_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return (Path(base) / "LuboStudio" / "quality_jobs").resolve()
    if os.name == "nt":
        return (Path.home() / "AppData" / "Local" / "LuboStudio" / "quality_jobs").resolve()
    return (Path.home() / ".local" / "state" / "LuboStudio" / "quality_jobs").resolve()


def firered_model_root() -> Path:
    configured = os.environ.get("LUBO_FIRERED_MODEL_DIR")
    return (Path(configured).expanduser().resolve() if configured
            else firered_runtime_root() / "fireredasr2-aed")


def firered_code_root() -> Path:
    configured = os.environ.get("LUBO_FIRERED_CODE_DIR")
    return (Path(configured).expanduser().resolve() if configured
            else firered_runtime_root() / "FireRedASR2S")


def firered_python_path() -> Path:
    configured = os.environ.get("LUBO_FIRERED_PYTHON")
    return (Path(configured).expanduser().resolve() if configured
            else firered_runtime_root() / "firered-venv" / "Scripts" / "python.exe")


def firered_worker_path() -> Path:
    configured = os.environ.get("LUBO_FIRERED_WORKER")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return application_directory() / "quality_support" / "firered_worker.py"
    return application_directory() / "scripts" / "firered_worker.py"


def _verified_firered_identity(profile: AsrModelProfile, model_root: Path) -> dict:
    marker = model_root / "lubo-model.json"
    if not marker.is_file():
        raise RuntimeError(
            f"FireRedASR2-AED 尚未完成固定版本安装：找不到模型校验清单。"
            f"请先安装到 {model_root}，或设置 LUBO_FIRERED_MODEL_DIR。"
        )
    try:
        details = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"FireRed 模型清单无法读取：{marker}") from exc
    if not isinstance(details, dict):
        raise RuntimeError(f"FireRed 模型清单顶层不是对象：{marker}")
    files = details.get("files")
    model_file = files.get("model.pth.tar") if isinstance(files, dict) else None
    expected = (details.get("repo_id") == profile.asr_repo
                and details.get("revision") == profile.asr_revision
                and details.get("code_repo") == profile.code_repo
                and details.get("code_revision") == profile.code_revision
                and isinstance(model_file, dict)
                and model_file.get("sha256") == profile.asr_model_sha256
                and model_file.get("size") == profile.asr_model_size
                and details.get("verified") is True)
    if not expected:
        raise RuntimeError(f"FireRed 模型清单与固定权重/代码 revision 不匹配：{marker}")
    return {
        "repo": profile.asr_repo,
        "revision": profile.asr_revision,
        "sha256": profile.asr_model_sha256,
        "code_repo": profile.code_repo,
        "code_revision": profile.code_revision,
    }


def model_identity(profile: AsrModelProfile, model_root: Path) -> dict:
    if profile.key == "firered_asr2_aed":
        asr = _verified_firered_identity(profile, model_root)
    else:
        asr = {"repo": profile.asr_repo, "revision": profile.asr_revision}
    return {
        "key": profile.key,
        "asr": asr,
        "aligner": ({"repo": profile.aligner_repo, "revision": profile.aligner_revision}
                    if profile.requires_forced_aligner else None),
    }


def effective_parameters(profile: AsrModelProfile, options, max_chars: int) -> dict:
    """Values that actually affect recognition/timing and therefore cache validity."""
    parameters = {
        "max_chars": max(1, int(max_chars)),
        "window_seconds": profile.max_window_seconds,
        "placement": "cpu",
        "dtype": "float32",
        "aligner_placement": "cpu" if profile.requires_forced_aligner else None,
        "aligner_dtype": "float32" if profile.requires_forced_aligner else None,
    }
    if profile.supports_context:
        parameters["context"] = str(getattr(options, "context", "") or "")
    if profile.supports_language:
        parameters["language"] = str(getattr(options, "language", "") or "")
    return parameters


def _terminate_process_tree(process: subprocess.Popen, grace_seconds: float = 4.0) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=grace_seconds, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                process.kill()
            except OSError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                raise


class QualityTranscriber:
    def __init__(self, options, log=None, engine: str = "quality"):
        self.options = options
        self.log = log or (lambda message: None)
        self.engine = engine
        self._process: subprocess.Popen | None = None
        self._cancel_path: Path | None = None

    def _command(self, job: Path, result: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--quality-worker", "--job", str(job), "--result", str(result)]
        return [sys.executable, "-m", "lubo.quality_worker", "--job", str(job), "--result", str(result)]

    def transcribe(self, wav_path, max_chars, on_progress=None, cancel=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if self.engine == "paraformer":
            return self._transcribe_paraformer(wav_path, max_chars, on_progress, cancel)
        profile = get_asr_profile(getattr(self.options, "quality_model", "qwen3"))
        model_root = (Path(os.environ.get("LUBO_QUALITY_MODEL_DIR", application_directory() / "quality_models"))
                      .expanduser().resolve())
        identity = model_identity(profile, firered_model_root())
        parameters = effective_parameters(profile, self.options, max_chars)
        self.log("正在计算音频指纹，之后可从中断位置继续")
        signature_before = source_signature(Path(wav_path))
        audio_sha256 = sha256_file(Path(wav_path))
        signature_after = source_signature(Path(wav_path))
        if signature_before != signature_after:
            raise RuntimeError("识别音频在计算指纹时发生变化，已停止以免缓存混入不同音频")
        fingerprint, fingerprint_input = recording_fingerprint(audio_sha256, identity, parameters)
        root = quality_job_root() / fingerprint[:2] / fingerprint
        root.mkdir(parents=True, exist_ok=True)
        # The per-attempt protocol files are unique; only the checkpoint store is shared.
        run_directory = root / "runs" / uuid.uuid4().hex
        run_directory.mkdir(parents=True, exist_ok=False)
        job_path = run_directory / "job.json"
        result_path = run_directory / "result.json"
        progress_path = run_directory / "progress.json"
        log_path = run_directory / "worker.log"
        self._cancel_path = run_directory / "cancel.requested"
        job = {
            "schema_version": 2,
            "mode": "recording",
            "wav_path": str(Path(wav_path).resolve()),
            "source_sha256": audio_sha256,
            "source_signature": signature_after,
            "job_fingerprint": fingerprint,
            "job_identity": fingerprint_input,
            "job_dir": str(root),
            "model_key": profile.key,
            "model_identity": identity,
            "effective_parameters": parameters,
            "max_chars": parameters["max_chars"],
            "model_root": str(model_root),
            "progress_path": str(progress_path),
            "log_path": str(log_path),
            "cancel_path": str(self._cancel_path),
        }
        if profile.supports_context:
            job["context"] = parameters["context"]
        if profile.supports_language:
            job["language"] = parameters["language"]
        atomic_write_json(job_path, job)
        self.log(f"高精度任务 {fingerprint[:12]}；持久任务目录：{root}")
        self.log(f"详细诊断日志：{log_path}")
        result = self._run_worker(job, job_path, result_path, progress_path,
                                  on_progress, cancel, log_path, "高精度识别")
        cues = [Cue(**cue) for cue in result["cues"]]
        self.log(f"高精度识别完成，共 {len(cues)} 条字幕")
        return cues

    def _transcribe_paraformer(self, wav_path, max_chars, on_progress, cancel) -> list[Cue]:
        run_directory = quality_job_root() / "paraformer" / uuid.uuid4().hex
        run_directory.mkdir(parents=True, exist_ok=False)
        job_path = run_directory / "job.json"
        result_path = run_directory / "result.json"
        progress_path = run_directory / "progress.json"
        log_path = run_directory / "worker.log"
        cancel_path = run_directory / "cancel.requested"
        job = {
            "schema_version": 1,
            "mode": "quick",
            "engine": "paraformer",
            "wav_path": str(Path(wav_path).resolve()),
            "max_chars": max(1, int(max_chars)),
            "device": getattr(self.options, "device", "cuda"),
            "model": getattr(self.options, "model", "paraformer-zh"),
            "vad_model": getattr(self.options, "vad_model", "fsmn-vad"),
            "punc_model": getattr(self.options, "punc_model", "ct-punc"),
            "chunk_seconds": getattr(self.options, "chunk_seconds", 120.0),
            "progress_path": str(progress_path),
            "cancel_path": str(cancel_path),
            "log_path": str(log_path),
        }
        atomic_write_json(job_path, job)
        self.log(f"快捷识别运行在受控子进程中；详细日志：{log_path}")
        result = self._run_worker(job, job_path, result_path, progress_path,
                                  on_progress, cancel, log_path, "快捷识别")
        try:
            cues = [Cue(**cue) for cue in result["cues"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"快捷识别结果中的字幕结构无效；诊断日志：{log_path}") from exc
        self.log(f"快捷识别完成，共 {len(cues)} 条字幕")
        return cues

    def _run_worker(self, job: dict, job_path: Path, result_path: Path,
                    progress_path: Path, on_progress, cancel, log_path: Path,
                    label: str) -> dict:
        self._cancel_path = Path(job["cancel_path"])
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_PROGRESS_BARS="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process_args = {"creationflags": flags}
        if os.name != "nt":
            process_args["start_new_session"] = True
        try:
            self._process = subprocess.Popen(
                self._command(job_path, result_path), cwd=application_directory(), env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **process_args,
            )
            last_message = None
            while self._process.poll() is None:
                if cancel is not None and cancel.is_set():
                    self._request_cancel()
                    raise Cancelled()
                try:
                    progress = json.loads(progress_path.read_text(encoding="utf-8"))
                    if on_progress is not None:
                        on_progress(float(progress["progress"]))
                    if progress["message"] != last_message:
                        self.log(progress["message"])
                        last_message = progress["message"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # The worker publishes progress atomically.
                time.sleep(.2)
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if not result_path.is_file():
                raise RuntimeError(
                    f"{label}子进程异常退出（{self._process.returncode}）；诊断日志：{log_path}"
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"{label}结果文件无效；诊断日志：{log_path}") from exc
            if not isinstance(result, dict):
                raise RuntimeError(f"{label}结果格式无效；诊断日志：{log_path}")
            if self._process.returncode or not result.get("ok") or not result.get("complete"):
                detail = result.get("error", "结果不完整")
                raise RuntimeError(f"{label}失败：{detail}；诊断日志：{log_path}")
            if on_progress is not None:
                on_progress(1.0)
            return result
        finally:
            self.close()

    def _request_cancel(self) -> None:
        if self._cancel_path is not None:
            try:
                self._cancel_path.write_text("cancel\n", encoding="ascii")
            except OSError:
                pass

    def close(self):
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            self._request_cancel()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
        self._cancel_path = None
