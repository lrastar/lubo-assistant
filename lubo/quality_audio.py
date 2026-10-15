"""Bounded recognition windows with context and an absolute audio timeline.

Silence is used to choose cut locations, never to discard audio. Each source
position belongs to exactly one core window; padding supplies acoustic context
without changing where recognized words belong on the recording timeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import wave

from lubo.wav_audio import SAMPLE_RATE, WavReader


@dataclass(frozen=True)
class AudioWindow:
    index: int
    core_start_ms: int
    core_end_ms: int
    read_start_ms: int
    read_end_ms: int

    @property
    def offset_seconds(self) -> float:
        return self.read_start_ms / 1000

    def owns_word(self, local_start: float, local_end: float) -> bool:
        midpoint_ms = (local_start + local_end) * 500 + self.read_start_ms
        return (self.core_start_ms <= midpoint_ms < self.core_end_ms
                or self.read_end_ms == self.core_end_ms == midpoint_ms)


def _quiet_cut(reader: WavReader, lower: int, preferred: int, upper: int) -> int:
    import numpy as np

    frame_ms = 20
    frame_samples = SAMPLE_RATE * frame_ms // 1000
    samples = reader.read_ms(lower, upper)
    count = len(samples) // frame_samples
    if not count:
        return preferred
    framed = samples[:count * frame_samples].astype(np.float32).reshape(count, frame_samples)
    quiet = np.sqrt(np.mean(framed * framed, axis=1)) <= 260  # roughly -42 dBFS
    choices = []
    begin = None
    for index, is_quiet in enumerate([*quiet, False]):
        if is_quiet and begin is None:
            begin = index
        elif not is_quiet and begin is not None:
            if (index - begin) * frame_ms >= 260:
                # Leave at least 120 ms of quiet on either side of the cut.
                first = lower + begin * frame_ms + 120
                last = lower + index * frame_ms - 120
                choices.append(min(max(preferred, first), last))
            begin = None
    return min(choices, key=lambda value: (abs(value - preferred), value)) if choices else preferred


def plan_windows(wav_path: Path, *, core_ms: int = 24000, search_ms: int = 4000,
                 context_ms: int = 1600) -> list[AudioWindow]:
    if not (1000 <= core_ms <= 30000 and 0 <= search_ms <= core_ms // 2 and context_ms >= 0):
        raise ValueError("Invalid recognition window settings")
    if core_ms + search_ms + 2 * context_ms > 40000:
        raise ValueError("Recognition windows must fit within the worker's 40-second limit")
    windows = []
    with WavReader(wav_path) as reader:
        start = 0
        while start < reader.total_ms:
            if reader.total_ms - start <= core_ms + search_ms:
                end = reader.total_ms
            else:
                preferred = start + core_ms
                end = _quiet_cut(reader, preferred - search_ms, preferred, preferred + search_ms)
            windows.append(AudioWindow(len(windows), start, end,
                                       max(0, start - context_ms), min(reader.total_ms, end + context_ms)))
            start = end
    return windows


def write_window(reader: WavReader, window: AudioWindow, path: Path) -> None:
    """Write only the bounded context window, in the model's PCM format."""
    samples = reader.read_ms(window.read_start_ms, window.read_end_ms)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())
