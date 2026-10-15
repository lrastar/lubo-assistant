"""Bounded PCM WAV access shared by all ASR models."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any
import wave

SAMPLE_RATE = 16000


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
