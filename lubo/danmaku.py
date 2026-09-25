"""B站/录播姬 XML 弹幕 → ASS（移植自 录播机/webhook_uploader 的 write_ass_from_xml）。"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from xml.etree import ElementTree

from lubo.config import DanmakuStyle
from lubo.subtitles import ass_time

BASE_HEIGHT = 1080
BASE_FONT_SIZE = 25  # B站默认字号

_ILLEGAL_XML_CHARS = re.compile(
    "[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)
_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")


@dataclass
class DanmakuItem:
    start: float
    mode: int
    size: int
    color: int
    text: str


def _is_legal_codepoint(code: int) -> bool:
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def clean_xml_text(text: str) -> str:
    """删除 XML 1.0 不允许的字符（包括 &#x1; 这类非法字符引用）。"""
    text = _ILLEGAL_XML_CHARS.sub("", text)

    def _ref(match: re.Match[str]) -> str:
        token = match.group(1)
        try:
            code = int(token[1:], 16) if token[0] in "xX" else int(token)
        except ValueError:
            return ""
        return match.group(0) if _is_legal_codepoint(code) else ""

    return _CHAR_REF.sub(_ref, text)


def parse_danmaku_item(element: ElementTree.Element) -> DanmakuItem | None:
    parts = element.attrib.get("p", "").split(",")
    if len(parts) < 4:
        return None
    text = element.text or ""
    if not text.strip():
        return None
    try:
        start = float(parts[0])
        mode = int(float(parts[1]))
        size = int(float(parts[2]))
        color = int(float(parts[3]))
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(start) or start < 0 or not 1 <= mode <= 9:
        return None
    if size <= 0 or not 0 <= color <= 0xFFFFFF:
        return None
    return DanmakuItem(start, mode, size, color, text)


def _collect(source: str | IO[bytes]) -> tuple[list[DanmakuItem], bool]:
    """解析弹幕；遇到截断/非法内容时保留已解析部分。返回 (弹幕, 是否完整解析)。"""
    items: list[DanmakuItem] = []
    try:
        for _event, element in ElementTree.iterparse(source, events=("end",)):
            if element.tag == "d":
                item = parse_danmaku_item(element)
                if item is not None:
                    items.append(item)
            element.clear()
    except ElementTree.ParseError:
        return items, False
    return items, True


def read_danmaku(xml_path: Path) -> list[DanmakuItem]:
    items, complete = _collect(str(xml_path))
    if complete:
        return items
    raw = Path(xml_path).read_bytes()
    cleaned = clean_xml_text(raw.decode("utf-8", errors="replace")).encode("utf-8")
    retry, _ = _collect(io.BytesIO(cleaned))
    return retry if len(retry) > len(items) else items


def ass_escape_text(value: str) -> str:
    # ElementTree 已经解码一次 XML 实体。再做 html.unescape 会把用户原本
    # 输入的字面量 "&amp;" 错误地改成 "&"。
    value = value.replace("\\", r"\\")
    value = value.replace("{", r"\{").replace("}", r"\}")
    return value.replace("\r\n", r"\N").replace("\n", r"\N").strip()


def ass_bgr(rgb: int) -> str:
    red = (rgb >> 16) & 0xFF
    green = (rgb >> 8) & 0xFF
    blue = rgb & 0xFF
    return f"{blue:02X}{green:02X}{red:02X}"


def ass_alpha_from_opacity(opacity: int) -> int:
    visible = max(0, min(100, opacity))
    return round(255 * (100 - visible) / 100)


def text_visual_width(text: str, font_size: int) -> int:
    units = 0.0
    for char in text:
        units += 0.55 if ord(char) < 128 else 1.0
    return max(1, int(units * font_size))


def choose_lane(lanes: list[float], start: float, duration: float) -> int:
    for index, available_at in enumerate(lanes):
        if available_at <= start:
            lanes[index] = start + duration
            return index
    index = min(range(len(lanes)), key=lambda item: lanes[item])
    lanes[index] = start + duration
    return index


@dataclass
class _Layout:
    width: int
    height: int
    scale: float
    alpha: int
    default_font_size: int
    line_height: int
    top_margin: int
    edge_pad: int
    outline: float
    lane_count: int

    def px(self, value: float, minimum: int = 1) -> int:
        return max(minimum, int(value * self.scale))


def _make_layout(width: int, height: int, style: DanmakuStyle) -> _Layout:
    scale = height / BASE_HEIGHT if height > 0 else 1.0

    def px(value: float, minimum: int = 1) -> int:
        return max(minimum, int(value * scale))

    default_font_size = px(max(18, BASE_FONT_SIZE * style.font_scale), 8)
    line_height = max(px(22), default_font_size + px(8))
    area_height = max(line_height, int(height * style.area_ratio))
    return _Layout(
        width=width,
        height=height,
        scale=scale,
        alpha=ass_alpha_from_opacity(style.opacity),
        default_font_size=default_font_size,
        line_height=line_height,
        top_margin=px(8),
        edge_pad=px(20),
        outline=round(2 * scale, 2),
        lane_count=max(1, area_height // line_height),
    )


def _header(layout: _Layout, style: DanmakuStyle) -> str:
    font = style.font.replace(",", " ").strip() or "Microsoft YaHei"
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {layout.width}\n"
        f"PlayResY: {layout.height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Danmaku,{font},{layout.default_font_size},"
        f"&H{layout.alpha:02X}FFFFFF,&H000000FF,&H80000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{layout.outline:g},0,7,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


class _LaneState:
    def __init__(self, count: int) -> None:
        self.scroll = [0.0] * count
        self.top = [0.0] * count
        self.bottom = [0.0] * count


def _dialogue(item: DanmakuItem, layout: _Layout, lanes: _LaneState, style: DanmakuStyle) -> str | None:
    escaped = ass_escape_text(item.text)
    if not escaped:
        return None
    font_size = max(layout.px(16, 6), int(item.size * style.font_scale * layout.scale))
    tags = rf"\fs{font_size}\c&H{ass_bgr(item.color)}&\alpha&H{layout.alpha:02X}&"
    center_x = layout.width // 2

    if item.mode == 4:
        duration = style.static_duration
        lane = choose_lane(lanes.bottom, item.start, duration)
        y = max(layout.top_margin, layout.height - layout.top_margin - lane * layout.line_height)
        override = rf"{{\an2\pos({center_x},{y}){tags}}}"
        layer = 1
    elif item.mode == 5:
        duration = style.static_duration
        lane = choose_lane(lanes.top, item.start, duration)
        y = layout.top_margin + lane * layout.line_height
        override = rf"{{\an8\pos({center_x},{y}){tags}}}"
        layer = 1
    else:
        duration = style.scroll_duration
        lane = choose_lane(lanes.scroll, item.start, duration)
        y = layout.top_margin + lane * layout.line_height
        visual_width = text_visual_width(escaped, font_size)
        if item.mode == 6:
            x1, x2 = -visual_width - layout.edge_pad, layout.width + layout.edge_pad
        else:
            x1, x2 = layout.width + layout.edge_pad, -visual_width - layout.edge_pad
        override = rf"{{\move({x1},{y},{x2},{y}){tags}}}"
        layer = 0

    return (
        f"Dialogue: {layer},{ass_time(item.start)},{ass_time(item.start + duration)},"
        f"Danmaku,,0,0,0,,{override}{escaped}\n"
    )


def xml_to_ass(xml_path: Path, ass_path: Path, width: int, height: int, style: DanmakuStyle) -> int:
    items = read_danmaku(Path(xml_path))
    # 模式 7/8/9（高级/代码/BAS 弹幕）内容不是纯文本，跳过
    items = [item for item in items if item.mode <= 6]
    items.sort(key=lambda item: item.start)

    layout = _make_layout(width, height, style)
    lanes = _LaneState(layout.lane_count)
    ass_path = Path(ass_path)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with ass_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(_header(layout, style))
        for item in items:
            line = _dialogue(item, layout, lanes, style)
            if line is not None:
                output.write(line)
                written += 1
    return written
