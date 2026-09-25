"""录播处理流水线：烧弹幕、拼接、识别语音、烧字幕。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from lubo.asr import Transcriber
from lubo.config import Settings, settings_path
from lubo.danmaku import xml_to_ass
from lubo.ffmpeg import Cancelled, FfmpegError, MediaInfo, has_encoder, probe, run_ffmpeg
from lubo.segments import Segment
from lubo.subtitles import read_srt, write_ass, write_srt


@dataclass
class Job:
    segments: list[Segment]
    settings: Settings
    output_dir: Path
    output_base: str
    strict_srt_source: bool = False


@dataclass
class PipelineCallbacks:
    log: Callable[[str], None]
    step: Callable[[str], None]
    progress: Callable[[float], None]


def _part_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.part{path.suffix}")


def _media_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and probe(path).duration > 0
    except (OSError, FfmpegError, ValueError):
        return False


def _wav_valid(path: Path) -> bool:
    import wave

    try:
        with wave.open(str(path), "rb") as wav:
            return (wav.getnframes() > 0 and wav.getnchannels() == 1
                    and wav.getframerate() == 16000 and wav.getsampwidth() == 2)
    except (OSError, EOFError, wave.Error):
        return False


def _concat_quote(path: Path) -> str:
    # ffconcat 使用单引号引路径；其中的单引号须先退出引用再转义。
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def _work_directory(job: Job) -> Path:
    root = os.environ.get("LUBO_WORK_DIR")
    if root:
        base = Path(root)
    else:
        local = os.environ.get("LOCALAPPDATA")
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "LuboStudio" / "work"
    identity = json.dumps({
        "segments": [str(Path(segment.video).resolve()) for segment in job.segments],
        "output_dir": str(Path(job.output_dir).resolve()),
        "output_base": job.output_base,
        "burn_danmaku": job.settings.steps.burn_danmaku,
        "danmaku": asdict(job.settings.danmaku),
        "intermediate_encode": {
            "encoder": job.settings.encode.encoder,
            "preset": job.settings.encode.preset,
            "quality": job.settings.encode.danmaku_cq,
            "audio_bitrate": job.settings.encode.audio_bitrate,
        },
    }, ensure_ascii=False, sort_keys=True)
    return base / hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _file_stamp(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "missing": True}
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".lubo_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class Pipeline:
    def __init__(self, job: Job, callbacks: PipelineCallbacks):
        self.job = job
        self.callbacks = callbacks
        self.cancel = threading.Event()
        self.work_dir = _work_directory(job)
        self._scratch = Path()
        self._encoder = ""
        self._preset = ""
        self._done_weight = 0.0
        self._total_weight = 1.0
        self._intermediates: set[Path] = set()
        self._source_snapshot: list[dict[str, object]] = []

    @staticmethod
    def _record_name(kind: str, path: Path) -> str:
        canonical = os.path.normcase(str(path.resolve()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{kind}-{digest}.json"

    def _job_record_path(self, kind: str, path: Path) -> Path:
        return settings_path().parent / "jobs" / self._record_name(kind, path)

    def _work_record_path(self, kind: str, path: Path) -> Path:
        return self.work_dir / self._record_name(kind, path)

    def _snapshot_sources(self) -> list[dict[str, object]]:
        return [{"video": _file_stamp(segment.video), "xml": _file_stamp(segment.xml)}
                for segment in self.job.segments]

    def _assert_sources_unchanged(self) -> None:
        if self._snapshot_sources() != self._source_snapshot:
            raise RuntimeError("处理期间原始视频或弹幕 XML 发生变化，请等待录播结束后重新生成字幕")

    def _srt_record(self, path: Path) -> dict:
        return {"version": 1, "srt": str(path.resolve()),
                "segments": self._source_snapshot}

    def _srt_record_state(self, path: Path) -> str:
        expected = self._srt_record(path)
        global_path = self._job_record_path("srt", path)
        work_path = self._work_record_path("srt", path)
        # 全局记录是权威来源；旧工作目录可能包含同名字幕的过期资料。
        record_path = global_path if global_path.exists() else work_path
        if not record_path.exists():
            return "missing"
        return "match" if _read_json(record_path) == expected else "mismatch"

    def _save_srt_record(self, path: Path) -> None:
        record = self._srt_record(path)
        _write_json(self._work_record_path("srt", path), record)
        _write_json(self._job_record_path("srt", path), record)

    def _render_record(self, target: Path, srt: Path, cover: Path | None) -> dict:
        encode = self.job.settings.encode
        steps = self.job.settings.steps
        return {
            "version": 1,
            "target": str(target.resolve()),
            "srt": str(srt.resolve()),
            "srt_sha256": _sha256_file(srt),
            "segments": self._source_snapshot,
            "cover": _file_stamp(cover),
            "burn_danmaku": steps.burn_danmaku,
            "concat": steps.concat,
            "danmaku_style": asdict(self.job.settings.danmaku),
            "subtitle_style": asdict(self.job.settings.subtitle),
            "intermediate_encode": {
                "encoder": self._encoder, "preset": self._preset,
                "cq": encode.danmaku_cq, "audio_bitrate": encode.audio_bitrate,
            },
            "final_encode": {
                "encoder": self._encoder, "preset": self._preset,
                "cq": encode.final_cq,
            },
        }

    def _render_record_matches(self, target: Path, record: dict) -> bool:
        path = self._job_record_path("render", target)
        return _read_json(path) == record and _media_valid(target)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("已取消")

    def _log(self, message: str) -> None:
        self.callbacks.log(message)

    def _step(self, message: str) -> None:
        self._check_cancel()
        self.callbacks.step(message)
        self._log(message)

    def _report(self, fraction: float = 0.0, weight: float = 0.0) -> None:
        value = (self._done_weight + max(0.0, min(1.0, fraction)) * weight) / self._total_weight
        self.callbacks.progress(max(0.0, min(1.0, value)))

    def _finish_unit(self, weight: float) -> None:
        self._done_weight += weight
        self._report()

    def _run_video(self, target: Path, args: list[str], duration: float,
                   weight: float, sources: list[Path] | None = None,
                   force: bool = False, cwd: Path | None = None) -> Path:
        self._check_cancel()
        part = _part_path(target)
        part.unlink(missing_ok=True)
        if self._reuse_video(target, sources or [], force=force):
            self._finish_unit(weight)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_ffmpeg(
                [*args, str(part)], duration,
                on_progress=lambda value: self._report(value, weight),
                cancel=self.cancel, log=self._log, cwd=cwd,
            )
            self._check_cancel()
            if not _media_valid(part):
                raise FfmpegError(f"处理结果无法读取或时长为零：{part}")
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        self._finish_unit(weight)
        return target

    def _reuse_video(self, target: Path, sources: list[Path], force: bool = False) -> bool:
        if self.job.settings.overwrite or force or not target.exists():
            return False
        if not _media_valid(target):
            self._log(f"已有文件无效，重新生成：{target}")
            target.unlink(missing_ok=True)
            return False
        target_mtime = target.stat().st_mtime_ns
        for source in sources:
            if source.is_file() and source.stat().st_mtime_ns > target_mtime:
                self._log(f"来源文件较新，重新生成：{target}")
                return False
        self._log(f"复用已完成文件：{target}")
        return True

    def _select_encoder(self) -> None:
        selected = self.job.settings.encode.encoder
        preset = self.job.settings.encode.preset
        if selected not in ("h264_nvenc", "hevc_nvenc", "libx264"):
            raise ValueError(f"不支持的视频编码器：{selected}")
        if selected.endswith("_nvenc") and not has_encoder(selected):
            self._log(f"FFmpeg 不支持 {selected}，改用 libx264（preset=medium）")
            selected, preset = "libx264", "medium"
        elif selected == "libx264" and preset.startswith("p") and preset[1:].isdigit():
            self._log("libx264 不使用 NVENC preset，改用 medium")
            preset = "medium"
        self._encoder, self._preset = selected, preset

    def _encode_args(self, quality: int) -> list[str]:
        common = ["-c:v", self._encoder, "-preset", self._preset]
        if self._encoder.endswith("_nvenc"):
            common += ["-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        else:
            common += ["-crf", str(quality)]
        return [*common, "-pix_fmt", "yuv420p"]

    @staticmethod
    def _fps_filter(fps: float) -> str:
        rate = Fraction(fps if 1 <= fps <= 120 else 30).limit_denominator(1001)
        return f"fps={rate.numerator}/{rate.denominator}"

    @staticmethod
    def _audio_needs_normalizing(infos: list[MediaInfo]) -> bool:
        signatures = {(info.acodec, info.sample_rate, info.channels) for info in infos}
        return (len(signatures) != 1 or any(not info.has_audio or info.acodec != "aac"
                                            for info in infos))

    @staticmethod
    def _sources_compatible(infos: list[MediaInfo], paths: list[Path]) -> bool:
        first = infos[0]
        reference = (first.vcodec, first.width, first.height, first.has_audio,
                     first.acodec, first.sample_rate, first.channels)
        return (
            first.vcodec in ("h264", "hevc")
            and len({path.suffix.lower() for path in paths}) == 1
            and all((info.vcodec, info.width, info.height, info.has_audio,
                     info.acodec, info.sample_rate, info.channels) == reference
                    and abs(info.fps - first.fps) < 0.02 for info in infos)
        )

    def _segment_names(self) -> list[str]:
        counts: dict[str, int] = {}
        names: list[str] = []
        for segment in self.job.segments:
            stem = segment.video.stem
            counts[stem] = counts.get(stem, 0) + 1
            names.append(stem if counts[stem] == 1 else f"{stem}.第{counts[stem]}段")
        return names

    def _transcode(self, source: Path, info: MediaInfo, target: Path,
                   xml: Path | None, target_info: MediaInfo, normalize_audio: bool,
                   weight: float) -> Path:
        self._check_cancel()
        sources = [source] + ([xml] if xml and xml.is_file() else [])
        filters = ["setpts=PTS-STARTPTS", self._fps_filter(target_info.fps)]
        if (info.width, info.height) != (target_info.width, target_info.height):
            width, height = target_info.width, target_info.height
            filters += [
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            ]
        filters.append("setsar=1")
        ass_name = "danmaku.ass"
        if xml and xml.is_file():
            # 只在需要实际转码时写 ASS；工作目录与滤镜参数均无特殊路径字符。
            count = xml_to_ass(xml, self._scratch / ass_name,
                               target_info.width, target_info.height,
                               self.job.settings.danmaku)
            self._log(f"{source.name}：读入 {count} 条弹幕")
            filters.append(f"ass={ass_name}")
        elif xml is not None:
            self._log(f"弹幕文件已不存在，继续处理：{source.name}")
        args = ["-i", str(source)]
        if not info.has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        args += ["-map", "0:v:0", "-map", "0:a:0" if info.has_audio else "1:a:0",
                 "-vf", ",".join(filters), *self._encode_args(self.job.settings.encode.danmaku_cq)]
        if normalize_audio:
            args += ["-c:a", "aac", "-ar", "48000", "-ac", "2",
                     "-b:a", self.job.settings.encode.audio_bitrate]
        else:
            args += ["-c:a", "copy"]
        if not info.has_audio:
            args.append("-shortest")
        args += ["-movflags", "+faststart"]
        return self._run_video(target, args, info.duration, weight, sources=sources, cwd=self._scratch)

    def _concat(self, paths: list[Path], target: Path, duration: float,
                weight: float) -> Path:
        list_path = self._scratch / "concat.txt"
        list_path.write_text("".join(f"file '{_concat_quote(path)}'\n" for path in paths),
                             encoding="utf-8", newline="\n")
        args = ["-f", "concat", "-safe", "0", "-i", str(list_path),
                "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                "-movflags", "+faststart"]
        return self._run_video(target, args, duration, weight, sources=paths,
                               cwd=self._scratch)

    def _reuse_wav(self, path: Path, source: Path) -> bool:
        if self.job.settings.overwrite or not path.exists():
            return False
        if not _wav_valid(path):
            self._log(f"已有 WAV 无效，重新抽取：{path}")
            path.unlink(missing_ok=True)
            return False
        if source.stat().st_mtime_ns > path.stat().st_mtime_ns:
            return False
        self._log(f"复用已抽取音频：{path}")
        return True

    def _extract_wav(self, source: Path, path: Path, duration: float,
                     weight: float) -> None:
        self._check_cancel()
        part = _part_path(path)
        part.unlink(missing_ok=True)
        if self._reuse_wav(path, source):
            self._finish_unit(weight)
            return
        try:
            run_ffmpeg(["-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(part)], duration,
                       on_progress=lambda value: self._report(value, weight),
                       cancel=self.cancel, log=self._log)
            self._check_cancel()
            if not _wav_valid(part):
                raise FfmpegError(f"抽出的 WAV 无效：{part}")
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
        self._finish_unit(weight)

    def _srt(self, source: Path, path: Path, duration: float,
             has_original_audio: bool, weight: float) -> Path:
        self._check_cancel()
        part = _part_path(path)
        part.unlink(missing_ok=True)
        options = self.job.settings.asr
        if path.is_file() and options.reuse_existing_srt and not self.job.settings.overwrite:
            state = self._srt_record_state(path)
            if state == "match" or (state == "missing" and not self.job.strict_srt_source):
                self._log(f"复用已有 SRT（保留手动修改）：{path}")
                self._finish_unit(weight)
                return path
            self._log(f"已有 SRT 的来源记录{('缺失' if state == 'missing' else '不匹配')}，重新识别：{path}")
        self._step(f"语音识别：{source.name}")
        wav = self.work_dir / f"{path.stem}.wav"
        self._intermediates.add(wav)
        if not has_original_audio:
            self._log("原始分段没有音频，生成空 SRT")
            cues = []
            self._finish_unit(weight)
        else:
            wav_weight = weight * 0.15
            self._extract_wav(source, wav, duration, wav_weight)
            transcriber = Transcriber(options, log=self._log)
            try:
                cues = transcriber.transcribe(
                    wav, self.job.settings.subtitle.max_chars,
                    on_progress=lambda value: self._report(value, weight - wav_weight),
                    cancel=self.cancel,
                )
                self._check_cancel()
            finally:
                transcriber.close()
            self._finish_unit(weight - wav_weight)
        try:
            write_srt(cues, part)
            self._check_cancel()
            self._assert_sources_unchanged()
            os.replace(part, path)
            self._save_srt_record(path)
        finally:
            part.unlink(missing_ok=True)
        self._log(f"字幕已生成：{path}（{len(cues)} 条）")
        return path

    def _burn_subtitles(self, source: Path, srt: Path, target: Path,
                        cover: Path | None, duration: float, weight: float) -> Path:
        self._check_cancel()
        ass = self._scratch / "subs.ass"
        info = probe(source)
        cues = read_srt(srt)
        if not cues or any(cue.start < 0 or cue.end <= cue.start for cue in cues):
            raise ValueError(f"字幕文件没有有效字幕或时间范围错误，请先修正 SRT：{srt}")
        record = self._render_record(target, srt, cover)
        reuse = not self.job.settings.overwrite and self._render_record_matches(target, record)
        if reuse:
            self._log(f"字幕及编码设置未变化，复用成片：{target}")
        write_ass(cues, ass, info.width, info.height, self.job.settings.subtitle)
        self._log(f"烧录 {len(cues)} 条字幕：{source.name}")
        args = ["-i", str(source)]
        attached = cover is not None and cover.is_file()
        if attached:
            args += ["-i", str(cover)]
        args += ["-filter_complex", "[0:v:0]ass=subs.ass[v]",
                 "-map", "[v]", "-map", "0:a:0?"]
        if attached:
            args += ["-map", "1:v:0"]
        args += self._encode_args(self.job.settings.encode.final_cq)
        args += ["-c:a", "copy"]
        if attached:
            args += ["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
        args += ["-movflags", "+faststart"]
        sources = [source, srt] + ([cover] if attached and cover is not None else [])
        result = self._run_video(target, args, duration, weight,
                                 sources=[] if reuse else sources,
                                 force=not reuse, cwd=self._scratch)
        if not reuse:
            self._assert_sources_unchanged()
            _write_json(self._job_record_path("render", target), record)
        return result

    def _cleanup_intermediates(self, final_paths: list[Path]) -> None:
        if self.job.settings.keep_intermediate:
            return
        protected = {path.resolve() for path in final_paths}
        for path in sorted(self._intermediates):
            if path.resolve() in protected:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._log(f"无法清理中间文件 {path}：{exc}")
        try:
            self.work_dir.rmdir()  # 仅删除空目录；绝不递归删除其它文件
        except OSError:
            pass

    def run(self, cancel: threading.Event) -> list[Path]:
        self.cancel = cancel
        self._check_cancel()
        if not self.job.segments:
            raise ValueError("请先添加至少一个分段")
        if not self.job.output_base or Path(self.job.output_base).name != self.job.output_base:
            raise ValueError("输出文件名前缀无效")
        output_dir = Path(self.job.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._select_encoder()
        self._step("检查录播分段")
        infos: list[MediaInfo] = []
        for segment in self.job.segments:
            self._check_cancel()
            try:
                info = probe(segment.video)
            except (FfmpegError, OSError) as exc:
                raise FfmpegError(f"分段损坏或无法读取：{segment.video}\n{exc}") from exc
            if info.duration <= 0 or info.width <= 0 or info.height <= 0:
                raise FfmpegError(f"分段没有有效视频/时长：{segment.video}")
            segment.info = info
            infos.append(info)
        self._source_snapshot = self._snapshot_sources()

        steps = self.job.settings.steps
        paths = [Path(segment.video).resolve() for segment in self.job.segments]
        total_duration = sum(info.duration for info in infos)
        normalize_without_danmaku = (not steps.burn_danmaku and steps.concat and len(paths) > 1
                                     and not self._sources_compatible(infos, paths))
        if normalize_without_danmaku:
            self._log("分段编码参数或封装不同，先统一转码再拼接")
        transcode = steps.burn_danmaku or normalize_without_danmaku
        concat_units = steps.concat and len(paths) > 1
        target_count = 1 if steps.concat else len(paths)
        self._total_weight = (
            (total_duration if transcode else 0.0)
            + (total_duration * 0.05 if concat_units else 0.0)
            + (total_duration * 0.5 if steps.asr else 0.0)
            + (total_duration if steps.burn_subtitles else 0.0)
        ) or 1.0
        self._done_weight = 0.0
        self._report()
        names = self._segment_names()
        bases = [self.job.output_base] if steps.concat else names
        for base in bases:
            self._intermediates.add(self._work_record_path("srt", output_dir / f"{base}.srt"))

        if steps.burn_subtitles and not steps.asr and self.job.strict_srt_source:
            for base in bases:
                srt = output_dir / f"{base}.srt"
                if not srt.is_file():
                    raise ValueError(f"找不到字幕文件，请先生成字幕：{srt}")
                if self._srt_record_state(srt) != "match":
                    raise ValueError(f"字幕来源与当前分段或顺序不匹配，请先重新生成字幕：{srt}")

        # 第二阶段已经成功过时，直接复用成片；默认清理后的工作目录不必重编码数小时。
        if steps.concat and steps.burn_subtitles and not steps.asr and not self.job.settings.overwrite:
            srt = output_dir / f"{self.job.output_base}.srt"
            if srt.is_file():
                cover = next((seg.cover for seg in self.job.segments
                              if seg.cover is not None and seg.cover.is_file()), None)
                target = output_dir / f"{self.job.output_base}.完整版.mp4"
                record = self._render_record(target, srt, cover)
                if self._render_record_matches(target, record):
                    self._assert_sources_unchanged()
                    suffix = ".烧录弹幕.GPU.mp4" if steps.burn_danmaku else ".合并准备.mp4"
                    if transcode:
                        self._intermediates.update(self.work_dir / f"{name}{suffix}" for name in names)
                    if concat_units:
                        self._intermediates.add(self.work_dir / f"{self.job.output_base}.合并.mp4")
                    self._intermediates.add(self.work_dir / f"{self.job.output_base}.wav")
                    self._cleanup_intermediates([target, srt])
                    self._log(f"字幕及编码设置未变化，复用成片：{target}")
                    self.callbacks.progress(1.0)
                    return [target, srt]

        with tempfile.TemporaryDirectory(prefix="lubo_") as scratch:
            self._scratch = Path(scratch)
            try:
                if transcode:
                    audio_norm = self._audio_needs_normalizing(infos)
                    if audio_norm:
                        self._log("统一音轨为 AAC 48kHz 双声道；无音频分段补静音轨")
                    encoded: list[Path] = []
                    for index, (segment, info, name) in enumerate(zip(self.job.segments, infos, names), 1):
                        self._step(f"{'烧录弹幕' if steps.burn_danmaku else '统一转码'} ({index}/{len(paths)})")
                        suffix = ".烧录弹幕.GPU.mp4" if steps.burn_danmaku else ".合并准备.mp4"
                        only_segment_output = (not concat_units and not steps.asr
                                               and not steps.burn_subtitles)
                        target_dir = output_dir if only_segment_output else self.work_dir
                        target = target_dir / f"{name}{suffix}"
                        if target_dir == self.work_dir:
                            self._intermediates.add(target)
                        xml = segment.xml if steps.burn_danmaku else None
                        if steps.burn_danmaku and xml is None:
                            self._log(f"{segment.video.name}：没有 XML，继续编码此分段")
                        encoded.append(self._transcode(paths[index - 1], info, target, xml,
                                                       infos[0], audio_norm, info.duration))
                    paths = encoded

                if concat_units:
                    self._step(f"顺序拼接 {len(paths)} 个分段")
                    # 只拼接时合并文件就是交付品；后续还要识别或烧字幕时放在工作盘。
                    merge_dir = self.work_dir if (steps.asr or steps.burn_subtitles) else output_dir
                    merged = merge_dir / f"{self.job.output_base}.合并.mp4"
                    if merge_dir == self.work_dir:
                        self._intermediates.add(merged)
                    paths = [self._concat(paths, merged, total_duration, total_duration * 0.05)]

                source_has_audio = [any(info.has_audio for info in infos)] if steps.concat else [info.has_audio for info in infos]
                durations = [total_duration] if steps.concat else [info.duration for info in infos]
                cover = next((seg.cover for seg in self.job.segments
                              if seg.cover is not None and seg.cover.is_file()), None)
                final_videos: list[Path] = []
                srts: list[Path] = []
                for index, (source, base, has_audio, duration) in enumerate(
                    zip(paths, bases, source_has_audio, durations), 1
                ):
                    self._check_cancel()
                    srt = output_dir / f"{base}.srt"
                    # 第二阶段关闭 ASR 时，第一阶段留下的 WAV 仍应在成功后清理。
                    self._intermediates.add(self.work_dir / f"{base}.wav")
                    if steps.asr:
                        self._step(f"语音识别 ({index}/{target_count})")
                        srts.append(self._srt(source, srt, duration, has_audio, duration * 0.5))
                    elif srt.is_file():
                        srts.append(srt)

                    if steps.burn_subtitles and srt.is_file():
                        self._step(f"烧录字幕 ({index}/{target_count})")
                        output = output_dir / f"{base}.完整版.mp4"
                        segment_cover = cover if steps.concat else self.job.segments[index - 1].cover
                        final_videos.append(self._burn_subtitles(source, srt, output, segment_cover,
                                                                 duration, duration))
                    else:
                        if steps.burn_subtitles:
                            self._log(f"没有 SRT，跳过字幕烧录：{base}")
                            self._finish_unit(duration)
                        final_videos.append(source)

                # 导出 SRT 阶段只交付 SRT；拼接-only 阶段交付合并视频。
                if steps.burn_subtitles:
                    deliverables = [*final_videos, *srts]
                elif steps.asr:
                    deliverables = srts
                else:
                    deliverables = final_videos
                self._check_cancel()
                self._cleanup_intermediates(deliverables)
                self.callbacks.progress(1.0)
                return deliverables
            finally:
                # TemporaryDirectory 清理滤镜 ASS/concat 清单；持久中间件只在成功后清理。
                self._scratch = Path()
