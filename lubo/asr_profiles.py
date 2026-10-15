"""Central model capabilities and pinned identity for high-quality ASR."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class AsrModelProfile:
    key: str
    display_name: str
    supports_context: bool
    supports_language: bool
    native_word_timestamps: bool
    requires_forced_aligner: bool
    max_window_seconds: int
    runtime: str
    asr_repo: str | None = None
    asr_revision: str | None = None
    asr_model_sha256: str | None = None
    asr_model_size: int | None = None
    aligner_repo: str | None = None
    aligner_revision: str | None = None
    code_repo: str | None = None
    code_revision: str | None = None


ASR_PROFILES: Mapping[str, AsrModelProfile] = MappingProxyType({
    "qwen3": AsrModelProfile(
        key="qwen3",
        display_name="Qwen3-ASR 1.7B + ForcedAligner",
        supports_context=True,
        supports_language=True,
        native_word_timestamps=False,
        requires_forced_aligner=True,
        max_window_seconds=40,
        runtime="application",
        asr_repo="Qwen/Qwen3-ASR-1.7B-hf",
        asr_revision="bcd2b5b7f32b480ab5790554cfa8347f246a14f3",
        aligner_repo="Qwen/Qwen3-ForcedAligner-0.6B-hf",
        aligner_revision="c07281df297b9905d24a508279258cccf987a064",
    ),
    "firered_asr2_aed": AsrModelProfile(
        key="firered_asr2_aed",
        display_name="FireRedASR2-AED",
        supports_context=False,
        supports_language=False,
        native_word_timestamps=True,
        # Keep the existing Qwen aligner for the first comparable quality pass.
        requires_forced_aligner=True,
        max_window_seconds=40,
        runtime="firered",
        asr_repo="FireRedTeam/FireRedASR2-AED",
        asr_revision="2304afed56eacfee6256dee5937ed22ffa0b64ec",
        asr_model_sha256="4677cbd30988d63ed3e777f6a42a1e5260a3865317f6e15e488bef40954f7054",
        asr_model_size=4731558506,
        code_repo="https://github.com/FireRedTeam/FireRedASR2S.git",
        code_revision="4e7d9aaf4482a47cec1724807026b9b151926eb5",
        aligner_repo="Qwen/Qwen3-ForcedAligner-0.6B-hf",
        aligner_revision="c07281df297b9905d24a508279258cccf987a064",
    ),
})


def get_asr_profile(key: str) -> AsrModelProfile:
    try:
        return ASR_PROFILES[key]
    except KeyError as exc:
        choices = ", ".join(ASR_PROFILES)
        raise ValueError(f"未知的高精度识别模型 {key!r}；可选：{choices}") from exc
