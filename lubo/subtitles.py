"""字幕：Cue、SRT 读写、字幕 ASS 生成。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lubo.config import SubtitleStyle

_SRT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?")
_ARROW = "-->"
_BLANK_LINES = re.compile(r"\n\s*\n")


@dataclass
class Cue:
    start: float   # 秒
    end: float
    text: str


def ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    hours, rest = divmod(total_cs, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_srt_time(value: str) -> float | None:
    match = _SRT_TIME.search(value.strip())
    if not match:
        return None
    hours, minutes, secs, frac = match.groups()
    ms = int((frac or "0").ljust(3, "0")[:3])
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs) + ms / 1000


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks: list[str] = []
    for cue in cues:
        text = _clean_text(cue.text)
        if not text:
            continue
        end = max(cue.end, cue.start)
        blocks.append(f"{len(blocks) + 1}\n{srt_time(cue.start)} {_ARROW} {srt_time(end)}\n{text}\n")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")  # 记事本另存的 ANSI 字幕


def _parse_block(lines: list[str]) -> Cue | None:
    for index, line in enumerate(lines):
        if _ARROW not in line:
            continue
        left, right = line.split(_ARROW, 1)
        start, end = parse_srt_time(left), parse_srt_time(right)
        if start is None or end is None:
            return None
        text = _clean_text("\n".join(lines[index + 1:]))
        return Cue(start, end, text) if text else None
    return None


def read_srt(path: Path) -> list[Cue]:
    text = _decode(Path(path).read_bytes()).lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in _BLANK_LINES.split(text):
        lines = [line for line in block.split("\n") if line.strip()]
        cue = _parse_block(lines)
        if cue is not None:
            cues.append(cue)
    return cues


def ass_color(value: str, alpha: int = 0) -> str:
    """"#RRGGBB" → "&HAABBGGRR"；无法解析时用白色。"""
    hex_value = value.strip().lstrip("#")
    if len(hex_value) == 3:
        hex_value = "".join(ch * 2 for ch in hex_value)
    try:
        rgb = int(hex_value[:6], 16) if len(hex_value) >= 6 else 0xFFFFFF
    except ValueError:
        rgb = 0xFFFFFF
    red, green, blue = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


def ass_escape_subtitle(text: str) -> str:
    # libass 没有反斜杠转义，替换成全角字符避免被当成样式标签
    text = text.replace("\\", "＼")
    text = text.replace("{", r"\{").replace("}", r"\}")
    return _clean_text(text).replace("\n", r"\N")


def _ass_header(width: int, height: int, style: SubtitleStyle) -> str:
    scale = height / 1080 if height > 0 else 1.0
    font = style.font.replace(",", " ").strip() or "Microsoft YaHei"
    font_size = max(8, round(height * style.size_ratio))
    outline = round(max(0.0, style.outline * scale), 2)
    margin_v = max(0, round(height * style.margin_ratio))
    margin_h = max(0, round(width * 0.03))
    primary = ass_color(style.primary_color)
    outline_color = ass_color(style.outline_color)
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,{font},{font_size},{primary},&H000000FF,{outline_color},&H80000000,"
        f"0,0,0,0,100,100,0,0,1,{outline:g},0,2,{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def write_ass(cues: list[Cue], path: Path, width: int, height: int, style: SubtitleStyle) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(_ass_header(width, height, style))
        for cue in cues:
            text = ass_escape_subtitle(cue.text)
            if not text:
                continue
            end = max(cue.end, cue.start)
            output.write(f"Dialogue: 0,{ass_time(cue.start)},{ass_time(end)},Sub,,0,0,0,,{{\\an2}}{text}\n")
