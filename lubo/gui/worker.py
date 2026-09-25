"""在 QThread 中运行 Pipeline；所有跨线程 UI 更新只通过信号。"""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, Signal, Slot

from lubo.ffmpeg import Cancelled, FfmpegError
from lubo.pipeline import Job, Pipeline, PipelineCallbacks


class PipelineWorker(QObject):
    log = Signal(str)
    step = Signal(str)
    progress = Signal(float)
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, job: Job) -> None:
        super().__init__()
        self._job = job
        self._cancel = threading.Event()
        self._last_permille = -1

    def cancel(self) -> None:
        """可从任意线程调用。"""
        self._cancel.set()

    @property
    def is_cancelling(self) -> bool:
        return self._cancel.is_set()

    def _emit_progress(self, value: float) -> None:
        # ffmpeg 进度回调非常频繁，只在千分位变化时发信号，避免淹没事件队列
        permille = max(0, min(1000, int(value * 1000)))
        if permille != self._last_permille:
            self._last_permille = permille
            self.progress.emit(permille / 1000)

    @Slot()
    def run(self) -> None:
        callbacks = PipelineCallbacks(log=self.log.emit, step=self.step.emit,
                                      progress=self._emit_progress)
        try:
            outputs = Pipeline(self._job, callbacks).run(self._cancel)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 任何错误都要回报给界面
            if self._cancel.is_set():
                self.cancelled.emit()
                return
            if not isinstance(exc, FfmpegError):
                self.log.emit("详细错误信息：\n" + traceback.format_exc().rstrip())
            self.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.finished.emit(list(outputs or []))
