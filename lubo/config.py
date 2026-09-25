from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

APP_DIR_NAME = "LuboStudio"


@dataclass
class DanmakuStyle:
    font: str = "Microsoft YaHei"
    font_scale: float = 1.4
    opacity: int = 40              # 0-100 可见度
    scroll_duration: float = 8.0
    static_duration: float = 5.0
    area_ratio: float = 0.35       # 弹幕占画面高度比例


@dataclass
class SubtitleStyle:
    font: str = "Microsoft YaHei"
    size_ratio: float = 0.055
    outline: float = 3.0
    margin_ratio: float = 0.04
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    max_chars: int = 22


@dataclass
class EncodeOptions:
    encoder: str = "h264_nvenc"
    preset: str = "p5"
    danmaku_cq: int = 21
    final_cq: int = 24
    audio_bitrate: str = "192k"


@dataclass
class AsrOptions:
    device: str = "cuda"
    model: str = "paraformer-zh"
    vad_model: str = "fsmn-vad"
    punc_model: str = "ct-punc"
    chunk_seconds: float = 120.0
    reuse_existing_srt: bool = True


@dataclass
class Steps:
    burn_danmaku: bool = True
    concat: bool = True
    asr: bool = True
    burn_subtitles: bool = True


@dataclass
class Settings:
    danmaku: DanmakuStyle = field(default_factory=DanmakuStyle)
    subtitle: SubtitleStyle = field(default_factory=SubtitleStyle)
    encode: EncodeOptions = field(default_factory=EncodeOptions)
    asr: AsrOptions = field(default_factory=AsrOptions)
    steps: Steps = field(default_factory=Steps)
    output_dir: str = ""
    keep_intermediate: bool = False
    overwrite: bool = False
    last_folder: str = ""
    last_segments: list[str] = field(default_factory=list)
    last_resources: list[str] = field(default_factory=list)
    sidecar_assignments: dict[str, dict[str, str]] = field(default_factory=dict)
    last_output_base: str = ""
    prefix_user_edited: bool = False
    window_geometry: str = ""


T = TypeVar("T")


def settings_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME / "settings.json"


def _coerce(value: Any, default: Any) -> Any:
    """按默认值的类型校验/转换；不兼容时返回默认值。"""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return default
    if isinstance(default, float):
        if isinstance(value, bool):
            return default
        return float(value) if isinstance(value, (int, float)) else default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else default
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return default
        return {
            video: {kind: path for kind, path in assignment.items()
                    if kind in ("xml", "cover") and isinstance(path, str)}
            for video, assignment in value.items()
            if isinstance(video, str) and isinstance(assignment, dict)
        }
    return default


def _from_dict(cls: type[T], data: Any) -> T:
    instance = cls()
    if not isinstance(data, dict):
        return instance
    for f in dataclasses.fields(instance):
        if f.name not in data:
            continue
        current = getattr(instance, f.name)
        raw = data[f.name]
        if dataclasses.is_dataclass(current):
            setattr(instance, f.name, _from_dict(type(current), raw))
        else:
            setattr(instance, f.name, _coerce(raw, current))
    return instance


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return Settings()
    return _from_dict(Settings, data)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dataclasses.asdict(settings), ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
