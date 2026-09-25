"""FunASR 封装：懒加载、按 VAD 静音边界分块识别、进度、取消、释放显存。"""
from __future__ import annotations

import gc
import importlib
import logging
import math
import os
import sys
import threading
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from lubo.config import AsrOptions
from lubo.ffmpeg import Cancelled
from lubo.subtitles import Cue

_logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
VAD_WINDOW_SECONDS = 120.0      # 有界读取；每次只把两分钟音频送入 VAD
MAX_ASR_CHUNK_SECONDS = 120.0  # 限制单次阻塞推理，取消可在块间生效
CHUNK_PAD_MS = 300              # 块边界向静音区外扩，避免切掉首尾字
VAD_PROGRESS_SHARE = 0.1        # VAD 占总进度的比例
DEFAULT_BATCH_SIZE_S = 120      # 4GB 显存下的保守批大小（秒）
MIN_BATCH_SIZE_S = 15
MIN_CUE_SECONDS = 0.2
SINGLE_CHAR_MERGE_GAP_SECONDS = 1.0
MAX_MERGED_CUE_SECONDS = 6.0

# 句尾去掉的标点；问号/感叹号/省略号保留，它们对字幕语气有意义
TRAILING_PUNCT = "，。、,.；;：:　 \t"


@dataclass
class Chunk:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


# ---------------------------------------------------------------- 纯函数（便于测试）

def merge_touching_segments(segments: list[list[int]], gap_ms: int = 20) -> list[list[int]]:
    """合并被 VAD 窗口边界切开的相邻语音段。"""
    merged: list[list[int]] = []
    for beg, end in sorted(segments):
        if merged and beg - merged[-1][1] <= gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([beg, end])
    return merged


def group_segments(segments: list[list[int]], chunk_ms: int, total_ms: int,
                   pad_ms: int = CHUNK_PAD_MS) -> list[Chunk]:
    """优先在静音处切块；连续讲话超过上限时也保证块长有界。"""
    chunk_ms = max(1, int(chunk_ms))
    bounded: list[list[int]] = []
    last_end = 0
    for beg, end in sorted(segments):
        beg = max(last_end, min(max(int(beg), 0), total_ms))
        end = min(max(int(end), 0), total_ms)
        while beg < end:
            part_end = min(end, beg + chunk_ms)
            bounded.append([beg, part_end])
            beg = part_end
        last_end = max(last_end, end)

    groups: list[list[list[int]]] = []
    for seg in bounded:
        if groups and seg[1] - groups[-1][0][0] <= chunk_ms:
            groups[-1].append(seg)
        else:
            groups.append([seg])

    chunks: list[Chunk] = []
    for i, group in enumerate(groups):
        prev_end = groups[i - 1][-1][1] if i > 0 else 0
        next_beg = groups[i + 1][0][0] if i + 1 < len(groups) else total_ms
        start = max(group[0][0] - pad_ms, prev_end, 0)
        end = min(group[-1][1] + pad_ms, next_beg, total_ms)
        if end > start:
            chunks.append(Chunk(int(start), int(end)))
    return chunks


def strip_trailing_punct(text: str) -> str:
    return text.strip().rstrip(TRAILING_PUNCT).strip()


def _is_latin(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def join_tokens(tokens: list[str]) -> str:
    """中文直接拼接，相邻的英文/数字 token 之间补空格。"""
    out = ""
    for tok in tokens:
        if out and tok and _is_latin(out[-1]) and _is_latin(tok[0]):
            out += " "
        out += tok
    return out


def _split_units(units: list[tuple[str, float, float]], max_chars: int) -> list[Cue]:
    """把 (文本, 开始, 结束) 单元均衡地拼成若干条不超过 max_chars 的 Cue。"""
    total = len(join_tokens([u[0] for u in units]))
    pieces = max(1, math.ceil(total / max_chars))
    target = min(max_chars, math.ceil(total / pieces))
    cues: list[Cue] = []
    current: list[tuple[str, float, float]] = []
    for unit in units:
        candidate = join_tokens([u[0] for u in current] + [unit[0]])
        if current and len(candidate) > target:
            cues.append(Cue(current[0][1], current[-1][2], join_tokens([u[0] for u in current])))
            current = []
        current.append(unit)
    if current:
        cues.append(Cue(current[0][1], current[-1][2], join_tokens([u[0] for u in current])))
    return cues


def _units_from_sentence(sentence: dict, body: str) -> list[tuple[str, float, float]]:
    """取字级单元（秒，相对块起点）。token 与时间戳数量不一致时按字数线性分配时间。"""
    stamps = sentence.get("timestamp") or []
    raw = sentence.get("raw_text")
    tokens = raw.split() if isinstance(raw, str) else []
    if stamps and len(tokens) == len(stamps):
        return [(tok, float(ts[0]) / 1000, float(ts[1]) / 1000) for tok, ts in zip(tokens, stamps)]

    chars = [c for c in body if not c.isspace()]
    if not chars:
        return []
    start = float(sentence.get("start", 0)) / 1000
    end = float(sentence.get("end", 0)) / 1000
    if stamps and len(stamps) == len(chars):
        return [(c, float(ts[0]) / 1000, float(ts[1]) / 1000) for c, ts in zip(chars, stamps)]
    step = max(end - start, 0.0) / len(chars)
    return [(c, start + i * step, start + (i + 1) * step) for i, c in enumerate(chars)]


def sentence_to_cues(sentence: dict, max_chars: int, offset: float = 0.0) -> list[Cue]:
    """把 FunASR 的 sentence_info 条目转换成 Cue（秒，已加 offset），超长时按字级时间戳拆分。"""
    text = str(sentence.get("text") or "")
    body = strip_trailing_punct(text)
    if not body:
        return []
    start = float(sentence.get("start", 0)) / 1000
    end = float(sentence.get("end", 0)) / 1000
    max_chars = max(1, int(max_chars))

    if len(body) <= max_chars:
        cues = [Cue(start, end, body)]
    else:
        units = _units_from_sentence(sentence, body)
        cues = _split_units(units, max_chars) if units else [Cue(start, end, body)]
        tail = text.strip()[len(body):].strip().rstrip(TRAILING_PUNCT)
        if tail and cues and len(cues[-1].text) + len(tail) <= max_chars:
            cues[-1].text += tail   # 保留句尾的 ？！
    return [Cue(c.start + offset, c.end + offset, c.text) for c in cues if c.text.strip()]


def finalize_cues(cues: list[Cue], duration: float, max_chars: int = 22) -> list[Cue]:
    """整理时间轴，并把模型意外切出的孤立单字接回相邻字幕。"""
    cues = sorted((c for c in cues if c.text.strip()), key=lambda c: (c.start, c.end))
    out: list[Cue] = []
    for cue in cues:
        start = min(max(cue.start, 0.0), duration)
        end = min(max(cue.end, start + MIN_CUE_SECONDS), duration)
        if out and start < out[-1].end:
            if start - out[-1].start >= 0.001:
                out[-1].end = round(start, 3)
            else:
                # 完全相同的起点常是相邻块重识别；保留先出现的一条。
                start = out[-1].end
        if end <= start:
            continue
        out.append(Cue(round(start, 3), round(end, 3), cue.text.strip()))
    merged: list[Cue] = []
    for index, cue in enumerate(out):
        if len(cue.text) == 1 and index + 1 < len(out):
            following = out[index + 1]
            if (following.start - cue.end <= SINGLE_CHAR_MERGE_GAP_SECONDS
                    and len(cue.text + following.text) <= max_chars
                    and following.end - cue.start <= MAX_MERGED_CUE_SECONDS):
                following.start = cue.start
                following.text = cue.text + following.text
                continue
        if len(cue.text) == 1 and merged:
            previous = merged[-1]
            if (cue.start - previous.end <= SINGLE_CHAR_MERGE_GAP_SECONDS
                    and len(previous.text + cue.text) <= max_chars
                    and cue.end - previous.start <= MAX_MERGED_CUE_SECONDS):
                previous.end = cue.end
                previous.text += cue.text
                continue
        merged.append(cue)
    return merged


class WavReader:
    """按时间范围读取 PCM WAV；内存只随 VAD 窗口或识别块增长。"""

    def __init__(self, path: Path):
        self._wav = wave.open(str(path), "rb")
        if self._wav.getcomptype() != "NONE" or self._wav.getsampwidth() != 2:
            self._wav.close()
            raise ValueError("识别音频必须是 16 位 PCM WAV；请先用 FFmpeg 转换")
        self.channels = self._wav.getnchannels()
        self.sample_rate = self._wav.getframerate()
        self.frames = self._wav.getnframes()
        if self.channels < 1 or self.sample_rate < 1:
            self._wav.close()
            raise ValueError("识别音频的声道数或采样率无效")
        self.duration = self.frames / self.sample_rate
        self.total_ms = math.ceil(self.duration * 1000)

    def __enter__(self) -> WavReader:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._wav.close()

    def read_ms(self, start_ms: int, end_ms: int) -> Any:
        """返回单声道 int16；非 16 kHz 输入也只在当前范围内重采样。"""
        import numpy as np

        start = min(max(round(start_ms * SAMPLE_RATE / 1000), 0),
                    round(self.frames * SAMPLE_RATE / self.sample_rate))
        end = min(max(round(end_ms * SAMPLE_RATE / 1000), start),
                  round(self.frames * SAMPLE_RATE / self.sample_rate))
        if end <= start:
            return np.empty(0, dtype=np.int16)

        source_start = min(self.frames, start * self.sample_rate // SAMPLE_RATE)
        source_end = min(self.frames,
                         math.ceil((end - 1) * self.sample_rate / SAMPLE_RATE) + 1)
        self._wav.setpos(source_start)
        raw = self._wav.readframes(source_end - source_start)
        samples = np.frombuffer(raw, dtype="<i2")
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).sum(axis=1, dtype=np.int32)
            samples = (samples // self.channels).astype(np.int16)
        if self.sample_rate == SAMPLE_RATE:
            return np.ascontiguousarray(samples)

        # 输出样本位置按整段音频的绝对时间计算，窗口之间不会累积采样误差。
        positions = (np.arange(start, end, dtype=np.float64)
                     * self.sample_rate / SAMPLE_RATE - source_start)
        interpolated = np.interp(positions, np.arange(len(samples)), samples)
        return np.rint(interpolated).astype(np.int16)


def _to_float(samples: Any) -> Any:
    import numpy as np

    return samples.astype(np.float32) / 32768.0


# ---------------------------------------------------------------- 日志转发

class _ForwardHandler(logging.Handler):
    def __init__(self, log: Callable[[str], None]):
        super().__init__(level=logging.WARNING)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log(f"[FunASR] {record.getMessage()}")
        except Exception:
            pass


def _ensure_std_streams() -> None:
    # pythonw 下 sys.stdout/stderr 为 None，第三方库的 print/tqdm 会崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------- Transcriber

class Transcriber:
    def __init__(self, options: AsrOptions, log: Callable[[str], None] | None = None):
        self.options = options
        self._log_fn = log
        self._model: Any = None
        self.device: str | None = None
        self.batch_size_s = DEFAULT_BATCH_SIZE_S

    # ---- 日志
    def _log(self, msg: str) -> None:
        if self._log_fn is not None:
            self._log_fn(msg)
        else:
            _logger.info(msg)

    @contextmanager
    def _route_logs(self) -> Iterator[None]:
        """把 FunASR（root logger）与 modelscope 的 WARNING 以上日志转发到 self._log。"""
        handler = _ForwardHandler(self._log)
        root = logging.getLogger()
        ms_logger = logging.getLogger("modelscope")
        old_ms_level = ms_logger.level
        root.addHandler(handler)
        ms_logger.setLevel(logging.WARNING)
        try:
            yield
        finally:
            root.removeHandler(handler)
            ms_logger.setLevel(old_ms_level)

    # ---- 加载
    def load(self, cancel: threading.Event | None = None) -> None:
        if self._model is not None:
            return
        self._check_cancel(cancel)
        _ensure_std_streams()
        with self._route_logs():
            import torch  # noqa: F401  (懒加载)

            device = self._pick_device()
            paths = self._resolve_models(cancel)
            self._check_cancel(cancel)
            try:
                self._model = self._build(paths, device)
            except Exception as exc:
                if not device.startswith("cuda"):
                    raise
                self._log(f"在 GPU 上加载模型失败（{exc}），改用 CPU")
                self._free_cuda()
                device = "cpu"
                self._model = self._build(paths, device)
            self.device = device
        self._check_cancel(cancel)
        self._log(f"语音识别模型已加载（设备：{'GPU' if device.startswith('cuda') else 'CPU'}）")

    def _pick_device(self) -> str:
        import torch

        wanted = (self.options.device or "cpu").strip().lower()
        if wanted.startswith("cuda"):
            if torch.cuda.is_available():
                self._log(f"使用 GPU 进行语音识别：{torch.cuda.get_device_name(0)}")
                return wanted
            self._log("CUDA 不可用，语音识别自动改用 CPU（速度会慢很多）")
        return "cpu"

    def _resolve_models(self, cancel: threading.Event | None = None) -> dict[str, str]:
        """把模型名解析为本地目录；缓存中没有的模型从 ModelScope 下载。解析失败则交给 FunASR 自行处理。"""
        names = {
            "model": self.options.model,
            "vad_model": self.options.vad_model,
            "punc_model": self.options.punc_model,
        }
        try:
            from funasr.download.name_maps_from_hub import name_maps_ms
            from modelscope.hub.snapshot_download import snapshot_download
        except Exception:
            return names

        self._prepare_modelscope_hub()

        resolved: dict[str, str] = {}
        announced = False
        for key, name in names.items():
            self._check_cancel(cancel)
            if not name or os.path.isdir(name):
                resolved[key] = name
                continue
            model_id = name_maps_ms.get(name, name)
            try:
                resolved[key] = snapshot_download(model_id, local_files_only=True)
                continue
            except Exception:
                pass
            if not announced:
                self._log("首次运行需要从 ModelScope 下载语音识别模型（约 2–3GB），请耐心等待……")
                announced = True
            self._log(f"正在下载模型：{model_id}")
            try:
                resolved[key] = self._download(snapshot_download, model_id)
                self._check_cancel(cancel)
                self._log(f"模型下载完成：{model_id}")
            except Cancelled:
                raise
            except Exception as exc:
                raise RuntimeError(f"下载模型 {model_id} 失败：{exc}") from exc
        return resolved

    @staticmethod
    def _prepare_modelscope_hub() -> None:
        """兼容 modelscope-hub 0.4.5 匿名下载时的未初始化 slots 字段。"""
        try:
            from modelscope_hub.config import HubConfig, get_default_config, set_default_config
        except ImportError:
            return
        try:
            get_default_config()
        except AttributeError as exc:
            if "_logged_out" not in str(exc):
                raise
            # 只在该上游缺陷出现时设匿名配置；公开模型无需账号。
            config = HubConfig(token="")
            config._logged_out = False
            config._endpoint_overridden = False
            set_default_config(config)

    def _download(self, snapshot_download: Callable[..., str], model_id: str) -> str:
        callback = self._make_progress_callback()
        if callback is not None:
            try:
                return snapshot_download(model_id, progress_callbacks=[callback])
            except TypeError:
                pass
        return snapshot_download(model_id)

    def _make_progress_callback(self) -> type | None:
        try:
            from modelscope.hub.callback import ProgressCallback
        except Exception:
            return None
        log = self._log
        lock = threading.Lock()

        class _LogProgress(ProgressCallback):  # type: ignore[misc, valid-type]
            def __init__(self, filename: str, file_size: int):
                super().__init__(filename, file_size)
                self.done = 0
                self.next_report = 0.25

            def update(self, size: int) -> None:
                with lock:
                    self.done += size
                    if self.file_size < 20 * 1024 * 1024:
                        return
                    frac = self.done / self.file_size
                    while frac >= self.next_report and self.next_report <= 1.0:
                        mb = self.file_size / 1024 / 1024
                        log(f"  下载 {Path(self.filename).name}（{mb:.0f}MB）：{self.next_report:.0%}")
                        self.next_report += 0.25

            def end(self) -> None:
                pass

        return _LogProgress

    def _build(self, paths: dict[str, str], device: str) -> Any:
        from funasr import AutoModel

        # FunASR 通过导入模块的副作用填充组件注册表。PyInstaller 的 PYZ
        # 归档不会像源码目录一样完整响应 pkgutil.walk_packages，因此显式
        # 加载当前三种模型配置需要的组件，避免打包版缺少 tokenizer 等注册项。
        for module in (
            "funasr.tokenizer.char_tokenizer",
            "funasr.frontends.wav_frontend",
            "funasr.models.seaco_paraformer.model",
            "funasr.models.sanm.encoder",
            "funasr.models.paraformer.decoder",
            "funasr.models.bicif_paraformer.cif_predictor",
            "funasr.models.specaug.specaug",
            "funasr.models.ct_transformer.model",
            "funasr.models.fsmn_vad_streaming.model",
            "funasr.models.fsmn_vad_streaming.encoder",
        ):
            importlib.import_module(module)

        return AutoModel(
            model=paths["model"],
            vad_model=paths["vad_model"],
            vad_kwargs={"max_single_segment_time": 30_000, "device": "cpu"},
            punc_model=paths["punc_model"],
            punc_kwargs={"device": "cpu"},
            device=device,
            ncpu=max(1, min(8, os.cpu_count() or 4)),
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
            log_level="WARNING",
        )

    # ---- 识别
    def transcribe(self, wav_path: Path, max_chars: int,
                   on_progress: Callable[[float], None] | None = None,
                   cancel: threading.Event | None = None) -> list[Cue]:
        self._check_cancel(cancel)
        self.load(cancel)
        wav_path = Path(wav_path)
        with WavReader(wav_path) as reader:
            total_ms = reader.total_ms
            duration = reader.duration
            self._log(f"音频时长 {_fmt_time(duration)}，开始语音活动检测（VAD）")

            with self._route_logs():
                segments = self._run_vad(reader, on_progress, cancel)
                speech_s = sum(e - b for b, e in segments) / 1000
                chunk_ms = max(10_000, min(int(self.options.chunk_seconds * 1000),
                                           int(MAX_ASR_CHUNK_SECONDS * 1000)))
                chunks = group_segments(segments, chunk_ms, total_ms)
                self._log(f"检测到语音 {len(segments)} 段（共 {_fmt_time(speech_s)}），分 {len(chunks)} 块识别")
                self._log("背景音乐或低声说话可能使 VAD 漏检语音；完成后请抽查字幕")

                cues: list[Cue] = []
                total_chunk_ms = sum(c.duration_ms for c in chunks) or 1
                done_ms = 0
                for i, chunk in enumerate(chunks, 1):
                    self._check_cancel(cancel)
                    self._log(f"识别第 {i}/{len(chunks)} 块（{_fmt_time(chunk.start_ms / 1000)} - "
                              f"{_fmt_time(chunk.end_ms / 1000)}）")
                    piece = reader.read_ms(chunk.start_ms, chunk.end_ms)
                    sentences = self._recognize(_to_float(piece), chunk.duration_ms, cancel)
                    offset = chunk.start_ms / 1000
                    for sentence in sentences:
                        cues.extend(sentence_to_cues(sentence, max_chars, offset))
                    done_ms += chunk.duration_ms
                    self._report(on_progress, VAD_PROGRESS_SHARE + (1 - VAD_PROGRESS_SHARE) * done_ms / total_chunk_ms)
                    self._check_cancel(cancel)

        result = finalize_cues(cues, duration, max_chars)
        self._report(on_progress, 1.0)
        self._log(f"语音识别完成，共 {len(result)} 条字幕")
        return result

    def _run_vad(self, reader: WavReader, on_progress: Callable[[float], None] | None,
                 cancel: threading.Event | None) -> list[list[int]]:
        model = self._model
        window_ms = int(VAD_WINDOW_SECONDS * 1000)
        n_windows = math.ceil(reader.total_ms / window_ms)
        segments: list[list[int]] = []
        for w in range(n_windows):
            self._check_cancel(cancel)
            offset_ms = w * window_ms
            piece = reader.read_ms(offset_ms, min((w + 1) * window_ms, reader.total_ms))
            if len(piece) < SAMPLE_RATE // 10:
                self._report(on_progress, VAD_PROGRESS_SHARE * (w + 1) / n_windows)
                continue
            res = model.inference(_to_float(piece), model=model.vad_model, kwargs=model.vad_kwargs)
            for item in res or []:
                for beg, end in item.get("value") or []:
                    if end > beg >= 0:
                        start = min(max(int(beg) + offset_ms, 0), reader.total_ms)
                        stop = min(max(int(end) + offset_ms, start), reader.total_ms)
                        if stop > start:
                            segments.append([start, stop])
            self._report(on_progress, VAD_PROGRESS_SHARE * (w + 1) / n_windows)
            self._check_cancel(cancel)
        return segments

    def _recognize(self, samples: Any, duration_ms: int,
                   cancel: threading.Event | None = None) -> list[dict]:
        """对一块音频做 VAD+ASR+标点，返回 sentence_info（毫秒，相对块起点）。显存不足时减小批大小重试。"""
        import torch

        while True:
            self._check_cancel(cancel)
            try:
                res = self._model.generate(
                    input=samples,
                    sentence_timestamp=True,
                    output_timestamp=True,
                    return_raw_text=True,
                    batch_size_s=self.batch_size_s,
                )
                break
            except torch.cuda.OutOfMemoryError:
                self._free_cuda()
                if self.batch_size_s <= MIN_BATCH_SIZE_S:
                    raise
                self.batch_size_s = max(MIN_BATCH_SIZE_S, self.batch_size_s // 2)
                self._log(f"显存不足，批大小降为 {self.batch_size_s} 秒后重试")
        self._check_cancel(cancel)
        sentences: list[dict] = []
        for item in res or []:
            info = item.get("sentence_info")
            if info:
                sentences.extend(info)
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            stamps = item.get("timestamp") or []
            if stamps:
                start, end = stamps[0][0], stamps[-1][1]
            else:
                start, end = 0, duration_ms
                self._log("模型未返回此块的时间戳，字幕时间按块长估算")
            sentences.append({"text": text, "raw_text": item.get("raw_text"),
                              "timestamp": stamps, "start": start, "end": end})
        return sentences

    # ---- 工具
    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    @staticmethod
    def _report(on_progress: Callable[[float], None] | None, value: float) -> None:
        if on_progress is not None:
            on_progress(min(max(value, 0.0), 1.0))

    @staticmethod
    def _free_cuda() -> None:
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def close(self) -> None:
        if self._model is None:
            return
        self._model = None
        self._free_cuda()
        self._log("已释放语音识别模型")
