"""Own construction, snapshots, and the lifetime of background pipeline jobs."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from lubo.asr_profiles import get_asr_profile
from lubo.config import Settings
from lubo.gui.worker import PipelineWorker
from lubo.pipeline import Job
from lubo.segments import Segment, default_output_base


class JobController(QObject):
    """Create immutable run snapshots and keep workers alive until QThread exits."""

    log = Signal(str)
    step = Signal(str)
    progress = Signal(float)
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()
    runningChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None, *, worker_factory=None) -> None:
        super().__init__(parent)
        self._worker_factory = worker_factory or PipelineWorker
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._active_mode: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    @property
    def is_cancelling(self) -> bool:
        return bool(self._worker is not None and getattr(self._worker, "is_cancelling", False))

    @property
    def active_mode(self) -> str | None:
        return self._active_mode

    def build_job(self, segments: list[Segment], settings: Settings,
                  output_dir: Path, output_base: str, mode: str | None = None) -> Job:
        """Snapshot UI state and apply the two explicit subtitle workflow stages."""
        snapshot = copy.deepcopy(settings)
        job = Job(copy.deepcopy(segments), snapshot, Path(output_dir),
                  output_base or default_output_base(segments),
                  strict_srt_source=mode in ("generate", "burn"))
        if mode is not None:
            snapshot.steps.burn_danmaku = True
            snapshot.steps.concat = True
            snapshot.steps.asr = mode == "generate"
            snapshot.steps.burn_subtitles = mode == "burn"
            if mode == "generate":
                snapshot.keep_intermediate = True
            elif mode == "burn":
                # This phase consumes the SRT the user has reviewed.
                snapshot.asr.reuse_existing_srt = True

        if snapshot.steps.asr and snapshot.asr.engine == "quality":
            profile = get_asr_profile(snapshot.asr.quality_model)
            if not profile.supports_context and snapshot.asr.context.strip():
                job.warnings.append(
                    f"{profile.display_name}不支持专属词汇提示；本次已从任务快照中省略，该偏好仍保存在设置中。")
                snapshot.asr.context = ""
            if not profile.supports_language and snapshot.asr.language:
                job.warnings.append(
                    f"{profile.display_name}不支持强制语言；本次已从任务快照中省略，该偏好仍保存在设置中。")
                snapshot.asr.language = ""
        return job

    def start(self, job: Job, mode: str | None = None) -> bool:
        if self.is_running:
            return False

        thread = QThread(self)
        worker = self._worker_factory(job)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker
        self._active_mode = mode

        thread.started.connect(worker.run)
        worker.log.connect(self._forward_log)
        worker.step.connect(self._forward_step)
        worker.progress.connect(self._forward_progress)
        worker.finished.connect(self._forward_finished)
        worker.failed.connect(self._forward_failed)
        worker.cancelled.connect(self._forward_cancelled)
        # quit() is thread-safe; a direct connection lets the worker stop its
        # event loop as soon as its terminal signal has been emitted.
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)

        for warning in job.warnings:
            self.log.emit(warning)
        self.runningChanged.emit(True)
        thread.start()
        return True

    def cancel(self) -> bool:
        if self._worker is None or self.is_cancelling:
            return False
        cancel = getattr(self._worker, "cancel", None)
        if cancel is None:
            return False
        cancel()
        return True

    @Slot(str)
    def _forward_log(self, message: str) -> None:
        self.log.emit(message)

    @Slot(str)
    def _forward_step(self, message: str) -> None:
        self.step.emit(message)

    @Slot(float)
    def _forward_progress(self, value: float) -> None:
        self.progress.emit(value)

    @Slot(list)
    def _forward_finished(self, outputs: list) -> None:
        self.finished.emit(outputs)

    @Slot(str)
    def _forward_failed(self, message: str) -> None:
        self.failed.emit(message)

    @Slot()
    def _forward_cancelled(self) -> None:
        self.cancelled.emit()

    @Slot()
    def _on_thread_finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        # finished is emitted before deferred worker deletion and native thread
        # cleanup complete. Join before releasing its Python wrapper; otherwise
        # the GUI and worker threads can destroy it concurrently.
        thread.wait()
        self._thread = None
        self._worker = None
        self._active_mode = None
        thread.deleteLater()
        self.runningChanged.emit(False)
