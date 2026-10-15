from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from lubo.asr_profiles import ASR_PROFILES

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
    engine: str = "quality"
    context: str = ""
    language: str = ""
    quality_model: str = "qwen3"


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
    theme: str = "dark"
    schema_version: int = 2
    load_warnings: list[str] = field(default_factory=list, repr=False, compare=False)


T = TypeVar("T")
SETTINGS_SCHEMA_VERSION = 2


def settings_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME / "settings.json"


def _coerce(value: Any, default: Any) -> tuple[Any, bool]:
    """Convert primitive config values and report whether the conversion was valid."""
    if isinstance(default, bool):
        valid = isinstance(value, bool)
        return (value if valid else default), valid
    if isinstance(default, int):
        if isinstance(value, bool):
            return default, False
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value), True
        return default, False
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default, False
        try:
            converted = float(value)
        except OverflowError:
            return default, False
        return (converted, True) if math.isfinite(converted) else (default, False)
    if isinstance(default, str):
        valid = isinstance(value, str)
        return (value if valid else default), valid
    if isinstance(default, list):
        if not isinstance(value, list):
            return default, False
        converted = [item for item in value if isinstance(item, str)]
        return converted, len(converted) == len(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return default, False
        converted = {
            video: {kind: path for kind, path in assignment.items()
                    if kind in ("xml", "cover") and isinstance(path, str)}
            for video, assignment in value.items()
            if isinstance(video, str) and isinstance(assignment, dict)
        }
        return converted, converted == value
    return default, False


def _from_dict(cls: type[T], data: Any, warnings: list[str], prefix: str = "") -> T:
    instance = cls()
    if not isinstance(data, dict):
        if prefix:
            warnings.append(f"{prefix} 配置结构无效，已恢复默认值")
        return instance
    for f in dataclasses.fields(instance):
        if f.name == "load_warnings" or f.name not in data:
            continue
        current = getattr(instance, f.name)
        raw = data[f.name]
        label = f"{prefix}.{f.name}" if prefix else f.name
        if dataclasses.is_dataclass(current):
            setattr(instance, f.name, _from_dict(type(current), raw, warnings, label))
        else:
            value, valid = _coerce(raw, current)
            if not valid:
                warnings.append(f"{label} 类型无效，已恢复默认值")
            setattr(instance, f.name, value)
    return instance


def _migrate_settings(data: Any, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        warnings.append("配置根节点无效，已使用默认设置")
        return {"schema_version": SETTINGS_SCHEMA_VERSION}
    migrated = dict(data)
    version = migrated.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        warnings.append("配置版本号无效，按旧版配置迁移")
        version = 1
    if version < 1:
        warnings.append("配置版本号过低，按旧版配置迁移")
        version = 1
    if version < 2:
        # Historical high-accuracy settings selected Qwen through `engine=quality`.
        # Keep every other preference and add its explicit profile key.
        asr = migrated.get("asr")
        if isinstance(asr, dict):
            asr = dict(asr)
            asr.setdefault("quality_model", "qwen3")
            migrated["asr"] = asr
        warnings.append("设置已从旧版格式迁移；保留已有识别方案和偏好")
    elif version > SETTINGS_SCHEMA_VERSION:
        warnings.append("配置来自较新版本；可识别的设置已加载，其余项目使用默认值")
    migrated["schema_version"] = SETTINGS_SCHEMA_VERSION
    return migrated


def _enum(value: str, choices: tuple[str, ...], default: str,
          label: str, warnings: list[str]) -> str:
    if value not in choices:
        warnings.append(f"{label}={value!r} 不受支持，已改用 {default!r}")
        return default
    return value


def _range(value: int | float, minimum: int | float, maximum: int | float,
           default: int | float, label: str, warnings: list[str]) -> int | float:
    finite = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not finite or not minimum <= value <= maximum):
        warnings.append(f"{label} 超出允许范围 {minimum}–{maximum}，已恢复默认值 {default}")
        return default
    return value


def _validate_settings(settings: Settings, warnings: list[str]) -> Settings:
    s = settings
    s.schema_version = SETTINGS_SCHEMA_VERSION
    s.theme = _enum(s.theme, ("dark", "light"), "dark", "theme", warnings)
    s.asr.engine = _enum(s.asr.engine, ("quality", "paraformer"), "quality", "asr.engine", warnings)
    s.asr.quality_model = _enum(
        s.asr.quality_model, tuple(ASR_PROFILES), "qwen3", "asr.quality_model", warnings)
    s.asr.device = _enum(s.asr.device, ("cpu", "cuda"), "cuda", "asr.device", warnings)
    s.asr.language = _enum(
        s.asr.language, ("", "Chinese", "English", "Japanese", "Cantonese"),
        "", "asr.language", warnings)
    s.encode.encoder = _enum(
        s.encode.encoder, ("h264_nvenc", "hevc_nvenc", "libx264"),
        "h264_nvenc", "encode.encoder", warnings)
    s.encode.audio_bitrate = _enum(
        s.encode.audio_bitrate, ("128k", "160k", "192k", "256k", "320k"),
        "192k", "encode.audio_bitrate", warnings)
    presets = tuple(f"p{i}" for i in range(1, 8)) if s.encode.encoder.endswith("_nvenc") else (
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
    preset_default = "p5" if s.encode.encoder.endswith("_nvenc") else "medium"
    s.encode.preset = _enum(s.encode.preset, presets, preset_default, "encode.preset", warnings)

    ranges = (
        (s.danmaku, "font_scale", 0.5, 4.0),
        (s.danmaku, "opacity", 0, 100),
        (s.danmaku, "scroll_duration", 1.0, 30.0),
        (s.danmaku, "static_duration", 1.0, 30.0),
        (s.danmaku, "area_ratio", 0.05, 1.0),
        (s.subtitle, "size_ratio", 0.01, 0.2),
        (s.subtitle, "outline", 0.0, 10.0),
        (s.subtitle, "margin_ratio", 0.0, 0.3),
        (s.subtitle, "max_chars", 6, 80),
        (s.encode, "danmaku_cq", 0, 51),
        (s.encode, "final_cq", 0, 51),
        (s.asr, "chunk_seconds", 30.0, 120.0),
    )
    for owner, field_name, minimum, maximum in ranges:
        default = getattr(type(owner)(), field_name)
        label = f"{type(owner).__name__}.{field_name}"
        setattr(owner, field_name, _range(getattr(owner, field_name), minimum, maximum,
                                          default, label, warnings))
    return s


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        settings = Settings()
        settings.load_warnings.append(f"设置文件无法读取，已使用默认设置：{exc}")
        return settings
    warnings: list[str] = []
    migrated = _migrate_settings(data, warnings)
    settings = _from_dict(Settings, migrated, warnings)
    settings = _validate_settings(settings, warnings)
    settings.load_warnings = warnings
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(settings)
    payload.pop("load_warnings", None)
    payload["schema_version"] = SETTINGS_SCHEMA_VERSION
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
