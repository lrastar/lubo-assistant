"""分段扫描：匹配弹幕 xml / 封面、解析文件名时间、排序。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lubo.ffmpeg import MediaInfo, probe

VIDEO_SUFFIXES = {".flv", ".mp4", ".mkv", ".ts"}
GENERATED_MARKERS = (".烧录弹幕", ".GPU", ".合并", ".完整版", ".burned", ".part")
COVER_SUFFIXES = (".cover.jpg", ".cover.jpeg", ".cover.png", ".cover.webp", ".jpg", ".jpeg", ".png", ".webp")
DANMAKU_SUFFIXES = (".xml",)

# "【xx／录播】2026_06_07-13_23_43 标题"
_SEPARATED_TIME = re.compile(
    r"(?<!\d)(\d{4})[_\-.](\d{2})[_\-.](\d{2})[-_ T](\d{2})[_\-.:](\d{2})[_\-.:](\d{2})(?!\d)"
)
# 自定义录播姬模板可使用两位年份，例如 "26-09-24_05-56-18"。
_SHORT_TIME = re.compile(
    r"(?<!\d)(\d{2})[-_](\d{2})[-_](\d{2})[-_ T](\d{2})[-_:](\d{2})[-_:](\d{2})(?!\d)"
)
# 录播姬默认 "录制-房间号-20260607-132343-123-标题"
_COMPACT_TIME = re.compile(r"(?<!\d)(\d{8})[-_](\d{6})(?:[-_](\d{3}))?(?!\d)")

_PAIRING_PATTERNS = (
    re.compile(r"^(.*?(?<!\d)\d{4}[_\-.]\d{2}[_\-.]\d{2}[-_ T]\d{2}[_\-.:]\d{2}[_\-.:]\d{2}(?:[-_.]\d{3})?)(?=$|[-_ .])"),
    re.compile(r"^(.*?(?<!\d)\d{2}[-_]\d{2}[-_]\d{2}[-_ T]\d{2}[-_:]\d{2}[-_:]\d{2}(?:[-_.]\d{3})?)(?=$|[-_ .])"),
    re.compile(r"^(.*?(?<!\d)\d{8}[-_]\d{6}(?:[-_]\d{3})?)(?=$|[-_ .])"),
)


@dataclass
class Segment:
    video: Path
    xml: Path | None
    cover: Path | None
    start_time: datetime | None   # 从文件名解析
    size: int                     # 字节
    info: MediaInfo | None = None # probe 结果（失败为 None，并记录 error）
    error: str | None = None


def _norm(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def is_source_video(path: Path) -> bool:
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return False
    stem = _norm(path.stem)
    return not any(stem.endswith(_norm(marker)) for marker in GENERATED_MARKERS)


def _try_datetime(*parts: int) -> datetime | None:
    try:
        return datetime(*parts)
    except ValueError:
        return None


def parse_start_time(name: str) -> datetime | None:
    name = unicodedata.normalize("NFKC", name)
    for match in _SEPARATED_TIME.finditer(name):
        parsed = _try_datetime(*(int(group) for group in match.groups()))
        if parsed is not None:
            return parsed
    for match in _SHORT_TIME.finditer(name):
        year, *rest = (int(group) for group in match.groups())
        parsed = _try_datetime(2000 + year, *rest)
        if parsed is not None:
            return parsed
    for match in _COMPACT_TIME.finditer(name):
        day, clock, millis = match.groups()
        parsed = _try_datetime(
            int(day[:4]), int(day[4:6]), int(day[6:]),
            int(clock[:2]), int(clock[2:4]), int(clock[4:]),
            int(millis or 0) * 1000,
        )
        if parsed is not None:
            return parsed
    return None


def _recording_key(stem: str) -> str | None:
    for pattern in _PAIRING_PATTERNS:
        match = pattern.match(stem)
        if match:
            return match.group(1)
    return None


def pairing_key(path: Path) -> str:
    return _recording_key(path.stem) or path.stem


def _normalize_suffixes(suffixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(suffix if suffix.startswith(".") else f".{suffix}" for suffix in suffixes)


def locate_sidecar(video: Path, suffixes: tuple[str, ...]) -> Path | None:
    video = Path(video)
    normalized = _normalize_suffixes(suffixes)
    stem = _norm(video.stem)
    try:
        entries = list(video.parent.iterdir())
    except OSError:
        return None

    # 录播姬以 Path.ChangeExtension(video, "xml") 生成弹幕；封面也是同 stem 的
    # .cover.<图片扩展名>。先做大小写/Unicode 规范化后的严格匹配。
    sidecars: list[tuple[int, str, Path]] = []
    for item in entries:
        if not item.is_file():
            continue
        name = _norm(item.name)
        for priority, suffix in enumerate(normalized):
            suffix = _norm(suffix)
            if name.endswith(suffix):
                sidecars.append((priority, name[: -len(suffix)], item))
                break

    exact = [(priority, item) for priority, sidecar_stem, item in sidecars if sidecar_stem == stem]
    if exact:
        return min(exact, key=lambda pair: (pair[0], _norm(pair[1].name)))[1]

    # 仅对带录制时间戳的文件做备选配对。断流时同一秒可能有多段；只要
    # 视频或 sidecar 不唯一，就宁可不配，也不把另一段弹幕烤进当前视频。
    recording_key = _recording_key(video.stem)
    if recording_key is None:
        return None
    key = _norm(recording_key)
    source_stems = {
        _norm(item.stem) for item in entries
        if item.is_file() and is_source_video(item)
        and _norm(_recording_key(item.stem) or "") == key
    }
    if len(source_stems) != 1:
        return None
    candidates = [item for _, sidecar_stem, item in sidecars
                  if _norm(_recording_key(sidecar_stem) or "") == key]
    return candidates[0] if len(candidates) == 1 else None


def make_segment(video: Path, probe_media: bool = True) -> Segment:
    video = Path(video)
    try:
        size = video.stat().st_size
    except OSError:
        size = 0
    segment = Segment(
        video=video,
        xml=locate_sidecar(video, DANMAKU_SUFFIXES),
        cover=locate_sidecar(video, COVER_SUFFIXES),
        start_time=parse_start_time(video.stem),
        size=size,
    )
    if probe_media:
        try:
            segment.info = probe(video)
        except Exception as exc:  # probe 失败只记录，不中断扫描
            segment.error = str(exc)
    return segment


def scan_folder(folder: Path, recursive: bool = False) -> list[Segment]:
    folder = Path(folder)
    paths = folder.rglob("*") if recursive else folder.glob("*")
    videos = [path for path in paths if path.is_file() and is_source_video(path)]
    return sort_segments([make_segment(video) for video in videos])


def _natural_key(value: str) -> list[object]:
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", _norm(value))]


def sort_segments(segments: list[Segment]) -> list[Segment]:
    def key(segment: Segment) -> tuple:
        timed = segment.start_time is not None
        return (
            0 if timed else 1,
            segment.start_time or datetime.min,
            _natural_key(segment.video.name),
            str(segment.video),
        )

    return sorted(segments, key=key)


def default_output_base(segments: list[Segment]) -> str:
    return segments[0].video.stem if segments else ""
