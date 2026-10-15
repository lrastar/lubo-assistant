"""Persisted pipeline artifacts: fingerprints, provenance, atomic records, and safe cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from lubo.config import settings_path
from lubo.asr_profiles import get_asr_profile
from lubo.ffmpeg import FfmpegError, probe


def default_work_root() -> Path:
    configured = os.environ.get("LUBO_WORK_DIR")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "LuboStudio" / "work"


def work_directory_for_job(job, root: Path | None = None) -> Path:
    """Return the stable per-job work directory used by existing installations."""
    root = Path(root) if root is not None else default_work_root()
    identity = json.dumps({
        "segments": [str(Path(segment.video).resolve()) for segment in job.segments],
        "output_dir": str(Path(job.output_dir).resolve()),
        "output_base": job.output_base,
        "burn_danmaku": job.settings.steps.burn_danmaku,
        "danmaku": asdict(job.settings.danmaku),
        "intermediate_encode": {
            "encoder": job.settings.encode.encoder,
            "preset": job.settings.encode.preset,
            "quality": job.settings.encode.danmaku_cq,
            "audio_bitrate": job.settings.encode.audio_bitrate,
        },
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return root / digest


def part_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.part{path.suffix}")


def file_stamp(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "missing": True}
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    """Publish a complete JSON record with a same-directory atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lubo_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def media_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and probe(path).duration > 0
    except (OSError, FfmpegError, ValueError):
        return False


def wav_valid(path: Path) -> bool:
    import wave

    try:
        with wave.open(str(path), "rb") as wav:
            return (wav.getnframes() > 0 and wav.getnchannels() == 1
                    and wav.getframerate() == 16000 and wav.getsampwidth() == 2)
    except (OSError, EOFError, wave.Error):
        return False


class ArtifactStore:
    """Own pipeline cache identity and the files it may safely remove.

    ``work_root`` and ``state_root`` are injectable so tests and alternate
    installations never touch the user's normal application data directory.
    The store only unlinks explicitly registered intermediates inside this
    job's work directory; it never recursively removes directories.
    """

    def __init__(self, job, *, work_root: Path | None = None,
                 state_root: Path | None = None) -> None:
        self.job = job
        self.work_dir = work_directory_for_job(job, work_root)
        self.state_root = Path(state_root) if state_root is not None else settings_path().parent
        self._source_snapshot: list[dict[str, object]] = []

    @staticmethod
    def _record_name(kind: str, path: Path) -> str:
        canonical = os.path.normcase(str(path.resolve()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{kind}-{digest}.json"

    def job_record_path(self, kind: str, path: Path) -> Path:
        return self.state_root / "jobs" / self._record_name(kind, path)

    def work_record_path(self, kind: str, path: Path) -> Path:
        return self.work_dir / self._record_name(kind, path)

    @staticmethod
    def commit_part(part: Path, target: Path) -> None:
        """Atomically publish a fully written `.part` artifact."""
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)

    def snapshot_sources(self) -> list[dict[str, object]]:
        return [{"video": file_stamp(segment.video), "xml": file_stamp(segment.xml)}
                for segment in self.job.segments]

    def capture_sources(self) -> None:
        self._source_snapshot = self.snapshot_sources()

    @property
    def source_snapshot(self) -> list[dict[str, object]]:
        return self._source_snapshot

    def assert_sources_unchanged(self) -> None:
        if self.snapshot_sources() != self._source_snapshot:
            raise RuntimeError("处理期间原始视频或弹幕 XML 发生变化，请等待录播结束后重新生成字幕")

    def _asr_fingerprint(self) -> dict[str, object]:
        fingerprint = {
            "version": 1,
            "options": asdict(self.job.settings.asr),
            "subtitle_max_chars": self.job.settings.subtitle.max_chars,
        }
        if self.job.settings.asr.engine == "quality":
            profile = get_asr_profile(self.job.settings.asr.quality_model)
            fingerprint["profile"] = {
                "key": profile.key,
                "asr_revision": profile.asr_revision,
                "asr_model_sha256": profile.asr_model_sha256,
                "aligner_revision": profile.aligner_revision,
                "code_revision": profile.code_revision,
            }
        return fingerprint

    def srt_record(self, path: Path) -> dict:
        return {
            "version": 2,
            "srt": str(path.resolve()),
            "segments": self._source_snapshot,
            "asr_fingerprint": self._asr_fingerprint(),
            "srt_sha256": sha256_file(path),
        }

    def _authoritative_srt_record(self, path: Path) -> tuple[Path | None, dict | None]:
        global_path = self.job_record_path("srt", path)
        work_path = self.work_record_path("srt", path)
        # Global record is authoritative; an older work directory may contain
        # stale provenance for an SRT with the same name.
        record_path = global_path if global_path.exists() else work_path
        return (record_path, read_json(record_path)) if record_path.exists() else (None, None)

    def srt_source_state(self, path: Path) -> str:
        """Check source identity only, allowing a user-edited SRT to be burned."""
        record_path, record = self._authoritative_srt_record(path)
        if record_path is None:
            return "missing"
        if (record is None or record.get("srt") != str(path.resolve())
                or record.get("segments") != self._source_snapshot):
            return "mismatch"
        return "match"

    def srt_reuse_state(self, path: Path) -> str:
        """Distinguish valid cache, manual edits, stale model, and legacy records."""
        source_state = self.srt_source_state(path)
        if source_state != "match":
            return source_state
        _, record = self._authoritative_srt_record(path)
        assert record is not None
        recorded_hash = record.get("srt_sha256")
        if not isinstance(recorded_hash, str):
            # Preserve legacy same-source subtitles when the user's explicit
            # reuse option is enabled; old records cannot prove their origin.
            return "legacy"
        if not path.is_file() or sha256_file(path) != recorded_hash:
            return "edited"
        if record.get("asr_fingerprint") != self._asr_fingerprint():
            return "stale"
        return "match"

    def save_srt_record(self, path: Path) -> None:
        record = self.srt_record(path)
        write_json_atomic(self.work_record_path("srt", path), record)
        write_json_atomic(self.job_record_path("srt", path), record)

    def render_record(self, target: Path, srt: Path, cover: Path | None,
                      encoder: str, preset: str) -> dict:
        encode = self.job.settings.encode
        steps = self.job.settings.steps
        return {
            "version": 1,
            "target": str(target.resolve()),
            "srt": str(srt.resolve()),
            "srt_sha256": sha256_file(srt),
            "segments": self._source_snapshot,
            "cover": file_stamp(cover),
            "burn_danmaku": steps.burn_danmaku,
            "concat": steps.concat,
            "danmaku_style": asdict(self.job.settings.danmaku),
            "subtitle_style": asdict(self.job.settings.subtitle),
            "intermediate_encode": {
                "encoder": encoder, "preset": preset,
                "cq": encode.danmaku_cq, "audio_bitrate": encode.audio_bitrate,
            },
            "final_encode": {
                "encoder": encoder, "preset": preset,
                "cq": encode.final_cq,
            },
        }

    def render_record_matches(self, target: Path, record: dict) -> bool:
        path = self.job_record_path("render", target)
        return read_json(path) == record and media_valid(target)

    @staticmethod
    def wav_record(source: Path) -> dict:
        return {"version": 2, "source": file_stamp(source),
                "clock": "aresample-async1000-first_pts0"}

    def cleanup(self, intermediates: set[Path], final_paths: list[Path],
                keep_intermediate: bool, log: Callable[[str], None]) -> None:
        if keep_intermediate:
            return
        protected = {Path(path).resolve() for path in final_paths}
        work_root = self.work_dir.resolve()
        for path in sorted(intermediates):
            try:
                resolved = Path(path).resolve()
                if resolved in protected:
                    continue
                if not resolved.is_relative_to(work_root):
                    log(f"保留工作目录外的文件，避免误删：{path}")
                    continue
                resolved.unlink(missing_ok=True)
            except OSError as exc:
                log(f"无法清理中间文件 {path}：{exc}")
        try:
            self.work_dir.rmdir()  # 删除空目录；绝不递归删除其它文件
        except OSError:
            pass
