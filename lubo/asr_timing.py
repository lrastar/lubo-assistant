"""Model-independent speech-text and subtitle timing utilities."""
from __future__ import annotations

import logging
import math
import re
import unicodedata

from lubo.subtitles import Cue

MIN_CUE_SECONDS = 0.2
SENTENCE_PAUSE_SECONDS = 0.6
TRAILING_PUNCT = "，。、,.；;：:　 \t"
SENTENCE_PUNCT = "，。！？；：、,.!?;:…"
_TEXT_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*|[^\s]")

_logger = logging.getLogger(__name__)


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


def _split_clause(units: list[tuple[str, float, float]], max_chars: int) -> list[Cue]:
    """优先在词语之间拆分长句，同时保留原始字词起止时间。"""
    if not units:
        return []
    full_text = join_tokens([unit[0] for unit in units])
    if len(strip_trailing_punct(full_text)) <= max_chars:
        return [Cue(units[0][1], units[-1][2], full_text)]
    try:
        import jieba
        jieba.setLogLevel(logging.WARNING)
        word_ends = {end for _, _, end in jieba.tokenize(full_text, HMM=False)}
    except ImportError:
        word_ends = set()
    prefix_ends = []
    prefix = ""
    for text, _, _ in units:
        prefix = join_tokens([prefix, text])
        prefix_ends.append(len(prefix))
    cues = []
    first = 0
    while first < len(units):
        remaining = len(strip_trailing_punct(join_tokens([u[0] for u in units[first:]])))
        if remaining <= max_chars:
            last = len(units)
        else:
            target = math.ceil(remaining / math.ceil(remaining / max_chars))
            choices = []
            current = ""
            for last in range(first + 1, len(units) + 1):
                current = join_tokens([current, units[last - 1][0]])
                length = len(strip_trailing_punct(current))
                if length > max_chars and last > first + 1:
                    break
                gap = units[last][1] - units[last - 1][2] if last < len(units) else 0
                choices.append((last, length, gap, prefix_ends[last - 1] in word_ends))
                if length > max_chars:
                    break  # An indivisible English word may exceed the display limit.
            natural = [choice for choice in choices if choice[3]]
            last = min(natural or choices, key=lambda c: (abs(c[1] - target), -c[2], c[0]))[0]
        text = join_tokens([unit[0] for unit in units[first:last]])
        cues.append(Cue(units[first][1], units[last - 1][2], text))
        first = last
    return cues


def _split_units(units: list[tuple[str, float, float]], max_chars: int) -> list[Cue]:
    """先按标点和真实语音停顿分句，再处理每一句的显示长度。"""
    cues: list[Cue] = []
    clause: list[tuple[str, float, float]] = []
    for unit in units:
        if clause and unit[1] - clause[-1][2] >= SENTENCE_PAUSE_SECONDS:
            cues.extend(_split_clause(clause, max_chars))
            clause = []
        clause.append(unit)
        tail = unit[0].rstrip('"\'”’」』）)]}')
        if tail and tail[-1] in SENTENCE_PUNCT:
            cues.extend(_split_clause(clause, max_chars))
            clause = []
    if clause:
        cues.extend(_split_clause(clause, max_chars))
    return cues


def _spoken_text(text: str) -> str:
    """用于对齐的文字；标点、空格和 BPE 分隔符不占声学时间戳。"""
    return "".join(ch for ch in text.replace("▁", "").replace("@@", "")
                   if not ch.isspace() and not unicodedata.category(ch).startswith("P")).casefold()


def _units_from_sentence(sentence: dict, body: str) -> list[tuple[str, float, float]]:
    """Map displayed text to real token times without inventing character times."""
    parts = _TEXT_TOKEN.findall(body)
    spoken = [part for part in parts if _spoken_text(part)]
    normalized = "".join(_spoken_text(part) for part in spoken)
    if not normalized:
        return []
    stamps = sentence.get("timestamp") or []
    times: list[tuple[float, float]] = []
    for stamp in stamps:
        try:
            start, end = float(stamp[0]) / 1000, float(stamp[1]) / 1000
            if not (math.isfinite(start) and math.isfinite(end) and end >= start >= 0):
                break
            if times and start < times[-1][0]:
                break
            times.append((start, end))
        except (TypeError, ValueError, IndexError):
            break
    if len(times) != len(stamps):
        times = []

    raw = sentence.get("raw_text")
    raw = raw if isinstance(raw, str) else ""
    candidates = [raw.split(), _TEXT_TOKEN.findall(raw), spoken, list(normalized)]
    char_times: list[tuple[float, float]] = []
    for candidate in candidates:
        tokens = [_spoken_text(token) for token in candidate if _spoken_text(token)]
        if times and len(tokens) == len(times) and "".join(tokens) == normalized:
            for token, (start, end) in zip(tokens, times):
                # A multi-character model token keeps its measured whole-token
                # interval. No evenly spaced per-character times are fabricated.
                char_times.extend([(start, end)] * len(token))
            break

    if not char_times:
        _logger.warning("此句缺少可映射的真实 token 时间戳，将保留句级时间：%s", body[:40])
        return []

    units: list[tuple[str, float, float]] = []
    cursor = 0
    prefix = ""
    for part in parts:
        size = len(_spoken_text(part))
        if not size:
            if units:
                text, start, end = units[-1]
                units[-1] = (text + part, start, end)
            else:
                prefix += part
            continue
        start, end = char_times[cursor][0], char_times[cursor + size - 1][1]
        if units and start == units[-1][1] and end == units[-1][2]:
            prior, prior_start, prior_end = units[-1]
            units[-1] = (prior + prefix + part, prior_start, prior_end)
        else:
            units.append((prefix + part, start, end))
        prefix = ""
        cursor += size
    return units


def sentence_to_cues(sentence: dict, max_chars: int, offset: float = 0.0) -> list[Cue]:
    """把 FunASR 的 sentence_info 条目转换成 Cue（秒，已加 offset），超长时按字级时间戳拆分。"""
    text = str(sentence.get("text") or "")
    body = text.strip()
    if not body:
        return []
    max_chars = max(1, int(max_chars))
    units = _units_from_sentence(sentence, body)
    if units:
        cues = _split_units(units, max_chars)
    else:
        start = float(sentence.get("start", 0)) / 1000
        end = float(sentence.get("end", 0)) / 1000
        cues = [Cue(start, end, body)]
    return [Cue(c.start + offset, c.end + offset, strip_trailing_punct(c.text))
            for c in cues if strip_trailing_punct(c.text)]


def finalize_cues(cues: list[Cue], duration: float, max_chars: int = 22) -> list[Cue]:
    """裁剪重叠和越界，保留声学起点与句界，不把后续字幕逐条推迟。"""
    cues = sorted((c for c in cues if c.text.strip()), key=lambda c: c.start)
    out: list[Cue] = []
    for cue in cues:
        start = min(max(cue.start, 0.0), duration)
        end = min(max(cue.end, start + MIN_CUE_SECONDS), duration)
        if out and start < out[-1].end:
            if start - out[-1].start >= 0.001:
                out[-1].end = round(start, 3)
            else:
                # 对齐模型会把短语气词量化成零时长，并与后句共用起点。
                # 仅凭相同起点不能判断重复；语气词重复也可能是实际说话内容。
                previous = out[-1]
                text = cue.text.strip()
                separator = "" if previous.text[-1:] in SENTENCE_PUNCT else "，"
                joined = previous.text + separator + text
                previous.text = joined if len(joined) <= max_chars else previous.text + "\n" + text
                previous.end = round(max(previous.end, end), 3)
                continue
        if end <= start:
            continue
        out.append(Cue(round(start, 3), round(end, 3), cue.text.strip()))
    return out


# Public names used by independent model adapters. The underscored aliases stay
# available through lubo.asr for older callers.
spoken_text = _spoken_text
units_from_sentence = _units_from_sentence
split_timed_units = _split_units
