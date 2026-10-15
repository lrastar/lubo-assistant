"""录播处理流水线：烧弹幕、拼接、识别语音、烧字幕。"""
from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

from lubo.asr_backend import create_transcriber
from lubo.artifact_store import (
    ArtifactStore, media_valid, part_path, read_json, wav_valid, write_json_atomic,
)
from lubo.config import Settings
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
    warnings: list[str] = field(default_factory=list, repr=False)


@dataclass
class PipelineCallbacks:
    log: Callable[[str], None]
    step: Callable[[str], None]
    progress: Callable[[float], None]


def _concat_quote(path: Path) -> str:
    # ffconcat 使用单引号引路径；其中的单引号须先退出引用再转义。
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


class Pipeline:
    def __init__(self, job: Job, callbacks: PipelineCallbacks, *,
                 work_root: Path | None = None, state_root: Path | None = None,
                 artifact_store: ArtifactStore | None = None):
        self.job = job
        self.callbacks = callbacks
        self.cancel = threading.Event()
        self.artifacts = artifact_store or ArtifactStore(
            job, work_root=work_root, state_root=state_root)
        self.work_dir = self.artifacts.work_dir
        self._scratch = Path()
        self._encoder = ""
        self._preset = ""
        self._done_weight = 0.0
        self._total_weight = 1.0
        self._intermediates: set[Path] = set()

    def _work_record_path(self, kind: str, path: Path) -> Path:
        """Compatibility for older pipeline callers; new code uses ArtifactStore."""
        return self.artifacts.work_record_path(kind, path)

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
        part = part_path(target)
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
            if not media_valid(part):
                raise FfmpegError(f"处理结果无法读取或时长为零：{part}")
            self.artifacts.commit_part(part, target)
        finally:
            part.unlink(missing_ok=True)
        self._finish_unit(weight)
        return target

    def _reuse_video(self, target: Path, sources: list[Path], force: bool = False) -> bool:
        if self.job.settings.overwrite or force or not target.exists():
            return False
        if not media_valid(target):
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
        if not wav_valid(path):
            self._log(f"已有 WAV 无效，重新抽取：{path}")
            path.unlink(missing_ok=True)
            return False
        if source.stat().st_mtime_ns > path.stat().st_mtime_ns:
            return False
        if read_json(self.artifacts.work_record_path("wav", path)) != self.artifacts.wav_record(source):
            self._log("识别音频的时间轴处理已更新，重新抽取音频")
            return False
        self._log(f"复用已抽取音频：{path}")
        return True

    def _extract_wav(self, source: Path, path: Path, duration: float,
                     weight: float) -> None:
        self._check_cancel()
        part = part_path(path)
        part.unlink(missing_ok=True)
        if self._reuse_wav(path, source):
            self._finish_unit(weight)
            return
        try:
            run_ffmpeg(["-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                        "-af", "aresample=async=1000:first_pts=0",
                        "-c:a", "pcm_s16le", str(part)], duration,
                       on_progress=lambda value: self._report(value, weight),
                       cancel=self.cancel, log=self._log)
            self._check_cancel()
            if not wav_valid(part):
                raise FfmpegError(f"抽出的 WAV 无效：{part}")
            self.artifacts.commit_part(part, path)
            write_json_atomic(self.artifacts.work_record_path("wav", path),
                              self.artifacts.wav_record(source))
        finally:
            part.unlink(missing_ok=True)
        self._finish_unit(weight)

    def _srt(self, source: Path, path: Path, duration: float,
             has_original_audio: bool, weight: float) -> Path:
        self._check_cancel()
        part = part_path(path)
        part.unlink(missing_ok=True)
        options = self.job.settings.asr
        if path.is_file() and options.reuse_existing_srt and not self.job.settings.overwrite:
            source_state = self.artifacts.srt_source_state(path)
            state = self.artifacts.srt_reuse_state(path)
            if source_state == "match" or (source_state == "missing" and not self.job.strict_srt_source):
                if state == "edited":
                    self._log(f"复用用户手动修改的 SRT：{path}")
                else:
                    self._log(f"复用已有 SRT（按设置保留现有字幕）：{path}")
                if state in ("stale", "legacy", "missing"):
                    self._log("已启用“已有同名 .srt 时跳过识别”，本次保留现有字幕；取消该选项后才会按当前识别模型重新生成")
                self._finish_unit(weight)
                return path
            self._log(f"已有 SRT 的来源记录{('缺失' if source_state == 'missing' else '不匹配')}，重新识别：{path}")
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
            transcriber = create_transcriber(options, log=self._log)
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
            self.artifacts.assert_sources_unchanged()
            self.artifacts.commit_part(part, path)
            self.artifacts.save_srt_record(path)
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
        record = self.artifacts.render_record(target, srt, cover, self._encoder, self._preset)
        reuse = not self.job.settings.overwrite and self.artifacts.render_record_matches(target, record)
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
            self.artifacts.assert_sources_unchanged()
            write_json_atomic(self.artifacts.job_record_path("render", target), record)
        return result

    def _cleanup_intermediates(self, final_paths: list[Path]) -> None:
        self.artifacts.cleanup(self._intermediates, final_paths,
                               self.job.settings.keep_intermediate, self._log)

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
        self.artifacts.capture_sources()

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
            self._intermediates.add(self.artifacts.work_record_path("srt", output_dir / f"{base}.srt"))

        if steps.burn_subtitles and not steps.asr and self.job.strict_srt_source:
            for base in bases:
                srt = output_dir / f"{base}.srt"
                if not srt.is_file():
                    raise ValueError(f"找不到字幕文件，请先生成字幕：{srt}")
                if self.artifacts.srt_source_state(srt) != "match":
                    raise ValueError(f"字幕来源与当前分段或顺序不匹配，请先重新生成字幕：{srt}")

        # 第二阶段已经成功过时，直接复用成片；默认清理后的工作目录不必重编码数小时。
        if steps.concat and steps.burn_subtitles and not steps.asr and not self.job.settings.overwrite:
            srt = output_dir / f"{self.job.output_base}.srt"
            if srt.is_file():
                cover = next((seg.cover for seg in self.job.segments
                              if seg.cover is not None and seg.cover.is_file()), None)
                target = output_dir / f"{self.job.output_base}.完整版.mp4"
                record = self.artifacts.render_record(target, srt, cover, self._encoder, self._preset)
                if self.artifacts.render_record_matches(target, record):
                    self.artifacts.assert_sources_unchanged()
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
