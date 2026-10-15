"""Durable recording identity, per-chunk commits, and process-safe job locks."""
from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any


CHECKPOINT_SCHEMA_VERSION = 1
PREPROCESSING_VERSION = "pcm16-mono-16000hz-v1"
CHUNKING_VERSION = "quiet-cut-core-24s-search-4s-context-1_6s-v1"


class BusyQualityJob(RuntimeError):
    """Another process already owns this exact audio/model job."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_signature(path: Path) -> dict[str, int]:
    stat = Path(path).stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": getattr(stat, "st_ino", 0),
    }


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def recording_fingerprint(audio_sha256: str, model_identity: dict,
                          effective_parameters: dict) -> tuple[str, dict]:
    identity = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "audio_sha256": audio_sha256,
        "model": model_identity,
        "effective_parameters": effective_parameters,
        "preprocessing_version": PREPROCESSING_VERSION,
        "chunking_version": CHUNKING_VERSION,
    }
    fingerprint = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return fingerprint, identity


def atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JobLock(AbstractContextManager["JobLock"]):
    """A nonblocking OS lock that is released automatically after process death."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> "JobLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b" ")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise BusyQualityJob(f"同一识别任务正在运行，锁文件：{self.path}") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise BusyQualityJob(f"同一识别任务正在运行，锁文件：{self.path}") from exc
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode("utf-8"))
            stream.flush()
            self._stream = stream
            return self
        except BaseException:
            stream.close()
            raise

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "JobLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


class RecordingCheckpoint:
    """Atomic recognition/alignment checkpoints for one immutable job identity."""

    def __init__(self, root: Path, fingerprint: str, identity: dict,
                 source_path: Path | None = None,
                 expected_source_signature: dict[str, int] | None = None):
        self.root = Path(root)
        self.fingerprint = fingerprint
        self.identity = identity
        self.source_path = Path(source_path) if source_path is not None else None
        self.expected_source_signature = expected_source_signature
        self.manifest_path = self.root / "manifest.json"
        self.chunks_dir = self.root / "chunks"
        self.recognition_dir = self.root / "recognition"
        self.alignment_dir = self.root / "alignment"
        self.failures_dir = self.root / "failures"
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(exist_ok=True)
        self.recognition_dir.mkdir(exist_ok=True)
        self.alignment_dir.mkdir(exist_ok=True)
        self.failures_dir.mkdir(exist_ok=True)
        self.cache_warnings: list[str] = []
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
            if self.manifest_path.exists():
                self.cache_warnings.append("manifest JSON 损坏，已忽略旧阶段索引")
        if manifest is not None and not isinstance(manifest, dict):
            self.cache_warnings.append("manifest 顶层不是对象，已忽略旧阶段索引")
            manifest = None
        if manifest and manifest.get("fingerprint") not in (None, fingerprint):
            raise ValueError(f"Checkpoint identity mismatch at {self.manifest_path}")
        if manifest and (manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
                         or manifest.get("identity") not in (None, identity)):
            self.cache_warnings.append("manifest schema 或 identity 无效，已忽略旧阶段索引")
            manifest = None
        if manifest and (not isinstance(manifest.get("chunks", {}), dict)
                         or not isinstance(manifest.get("stages", {}), dict)):
            self.cache_warnings.append("manifest 阶段索引结构损坏，已忽略旧阶段索引")
            manifest = None
        self.manifest = manifest or {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "identity": identity,
            "chunks": {},
            "stages": {"recognition": {}, "alignment": {}},
            "created_at": time.time(),
        }
        self.save_manifest()

    def invalidate_cache(self, reason: str) -> None:
        """Forget all derived audio and stage data for a changed source identity."""
        for directory in (self.chunks_dir, self.recognition_dir, self.alignment_dir):
            for path in directory.iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        self.manifest = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "identity": self.identity,
            "chunks": {},
            "stages": {"recognition": {}, "alignment": {}},
            "created_at": self.manifest.get("created_at", time.time()),
            "invalidated_at": time.time(),
            "invalidated_reason": reason,
        }
        self.save_manifest()

    def assert_source_unchanged(self) -> None:
        if self.source_path is None or self.expected_source_signature is None:
            return
        try:
            current = source_signature(self.source_path)
        except OSError as exc:
            reason = f"源音频在识别期间不可访问：{exc}"
        else:
            if current == self.expected_source_signature:
                return
            reason = "源音频在识别期间发生变化，已清除当前 fingerprint 的派生缓存"
        self.invalidate_cache(reason)
        raise RuntimeError(reason)

    def save_manifest(self) -> None:
        self.manifest["updated_at"] = time.time()
        atomic_write_json(self.manifest_path, self.manifest)

    def chunk_path(self, chunk_id: str) -> Path:
        return self.chunks_dir / f"{chunk_id}.wav"

    def remember_chunk(self, chunk_id: str, audio_sha256: str, duration_seconds: float) -> None:
        self.assert_source_unchanged()
        self.manifest.setdefault("chunks", {})[chunk_id] = {
            "sha256": audio_sha256,
            "duration_seconds": duration_seconds,
        }
        self.save_manifest()
        self.assert_source_unchanged()

    def verified_chunk(self, chunk_id: str) -> Path | None:
        self.assert_source_unchanged()
        path = self.chunk_path(chunk_id)
        chunks = self.manifest.get("chunks")
        entry = chunks.get(chunk_id) if isinstance(chunks, dict) else None
        if entry is not None and not isinstance(entry, dict):
            self.cache_warnings.append(f"chunks/{chunk_id} 索引结构无效，将重新生成音频块")
            return None
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        if not path.is_file() or not expected:
            return None
        try:
            return path if sha256_file(path) == expected else None
        except OSError:
            return None

    def stage_path(self, stage: str, chunk_id: str) -> Path:
        if stage not in ("recognition", "alignment"):
            raise ValueError(f"Unknown checkpoint stage: {stage}")
        directory = self.recognition_dir if stage == "recognition" else self.alignment_dir
        return directory / f"{chunk_id}.json"

    def read_stage(self, stage: str, chunk_id: str) -> dict | None:
        self.assert_source_unchanged()
        path = self.stage_path(stage, chunk_id)
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if path.exists():
                self.cache_warnings.append(f"{stage}/{chunk_id} 阶段 JSON 损坏，将重新执行")
            return None
        if not isinstance(item, dict):
            self.cache_warnings.append(f"{stage}/{chunk_id} 阶段缓存顶层无效，将重新执行")
            return None
        if (item.get("fingerprint") != self.fingerprint or item.get("chunk_id") != chunk_id
                or item.get("ok") is not True):
            return None
        value: Any = item.get("value")
        if not isinstance(value, dict):
            self.cache_warnings.append(f"{stage}/{chunk_id} 阶段结果结构无效，将重新执行")
            return None
        return value

    def commit_stage(self, stage: str, chunk_id: str, value: dict) -> None:
        self.assert_source_unchanged()
        if not isinstance(value, dict):
            raise TypeError("Checkpoint stage value must be an object")
        atomic_write_json(self.stage_path(stage, chunk_id), {
            "fingerprint": self.fingerprint,
            "chunk_id": chunk_id,
            "ok": True,
            "value": value,
            "committed_at": time.time(),
        })
        self.assert_source_unchanged()
        self.manifest.setdefault("stages", {}).setdefault(stage, {})[chunk_id] = "complete"
        self.save_manifest()
        self.assert_source_unchanged()

    def record_failure(self, stage: str, chunk_id: str, error: str) -> None:
        self.assert_source_unchanged()
        atomic_write_json(self.failures_dir / f"{stage}-{chunk_id}.json", {
            "fingerprint": self.fingerprint,
            "stage": stage,
            "chunk_id": chunk_id,
            "error": error,
            "recorded_at": time.time(),
        })
        self.manifest.setdefault("stages", {}).setdefault(stage, {})[chunk_id] = "failed"
        self.save_manifest()
