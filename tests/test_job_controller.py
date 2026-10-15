import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QEventLoop, QTimer, Signal, Slot
    from PySide6.QtWidgets import QApplication, QMessageBox
    from lubo.gui.job_controller import JobController
    from lubo.gui.main_window import MainWindow
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from lubo.config import Settings, save_settings
from lubo.segments import Segment


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is required for offscreen lifecycle tests")
class JobControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @staticmethod
    def segment(folder: Path) -> Segment:
        video = folder / "clip.mp4"
        video.write_bytes(b"clip")
        return Segment(video, None, None, None, video.stat().st_size)

    def test_job_is_a_settings_snapshot_and_firered_omits_unsupported_qwen_preferences(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            settings = Settings()
            settings.asr.quality_model = "firered_asr2_aed"
            settings.asr.context = "Qwen 专属词汇"
            settings.asr.language = "Chinese"
            controller = JobController()
            segment = self.segment(root)

            job = controller.build_job(
                [segment], settings, root / "output", "clip", "generate")

            self.assertEqual(job.settings.asr.context, "")
            self.assertEqual(job.settings.asr.language, "")
            self.assertEqual(settings.asr.context, "Qwen 专属词汇")
            self.assertEqual(settings.asr.language, "Chinese")
            self.assertTrue(job.settings.steps.asr)
            self.assertFalse(job.settings.steps.burn_subtitles)
            self.assertTrue(job.settings.keep_intermediate)
            self.assertTrue(job.strict_srt_source)
            self.assertEqual(len(job.warnings), 2)
            settings.subtitle.max_chars = 60
            self.assertEqual(job.settings.subtitle.max_chars, 22)

            segment.video = root / "mutated-after-snapshot.mp4"
            segment.size = 999
            self.assertEqual(job.segments[0].video, root / "clip.mp4")
            self.assertEqual(job.segments[0].size, 4)
            self.assertIsNot(job.segments[0], segment)

    def test_cancelled_worker_keeps_controller_running_until_qthread_finishes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            controller = JobController(worker_factory=_DelayedWorker)
            job = controller.build_job([self.segment(root)], Settings(), root, "clip")
            loop = QEventLoop()
            events: list[object] = []
            controller.cancelled.connect(lambda: events.append("cancelled"))
            controller.runningChanged.connect(
                lambda running: (events.append(running), loop.quit() if not running else None))

            self.assertTrue(controller.start(job))
            QTimer.singleShot(5, controller.cancel)
            QTimer.singleShot(2000, loop.quit)
            loop.exec()

            self.assertIn("cancelled", events)
            self.assertEqual(events[-1], False)
            self.assertFalse(controller.is_running)

    def test_window_close_waits_for_cancelled_worker_before_accepting_close(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            controller = JobController(worker_factory=_DelayedWorker)
            window = MainWindow(settings_file=root / "settings.json", job_controller=controller)
            window.show()
            job = controller.build_job([self.segment(root)], Settings(), root / "out", "clip")
            loop = QEventLoop()
            controller.runningChanged.connect(
                lambda running: QTimer.singleShot(100, loop.quit) if not running else None)

            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                self.assertTrue(controller.start(job))
                QTimer.singleShot(5, window.close)
                QTimer.singleShot(2500, loop.quit)
                loop.exec()

            self.assertFalse(controller.is_running)
            self.assertFalse(window.isVisible())
            self.assertTrue((root / "settings.json").is_file())
            window.deleteLater()

    def test_repeated_cancellation_releases_finished_worker_safely(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for attempt in range(25):
                with self.subTest(attempt=attempt):
                    controller = JobController(worker_factory=_DelayedWorker)
                    job = controller.build_job([self.segment(root)], Settings(), root, "clip")
                    loop = QEventLoop()
                    timeout = QTimer(loop)
                    timeout.setSingleShot(True)
                    timeout.timeout.connect(loop.quit)
                    controller.runningChanged.connect(
                        lambda running: loop.quit() if not running else None)
                    self.assertTrue(controller.start(job))
                    QTimer.singleShot(5, controller.cancel)
                    timeout.start(2000)
                    loop.exec()
                    timeout.stop()
                    self.assertFalse(controller.is_running)
                    self.app.processEvents()

    def test_profile_capabilities_disable_unsupported_fields_without_erasing_preferences(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "settings.json"
            settings = Settings()
            settings.asr.context = "保留的 Qwen 热词"
            settings.asr.language = "Chinese"
            save_settings(settings, path)
            window = MainWindow(settings_file=path)

            self.assertTrue(window.asr_context_edit.isEnabled())
            self.assertTrue(window.asr_language_combo.isEnabled())
            self.assertFalse(window.asr_device_combo.isEnabled())
            self.assertFalse(window.asr_chunk_seconds.isEnabled())

            index = window.quality_model_combo.findData("firered_asr2_aed")
            window.quality_model_combo.setCurrentIndex(index)
            self.app.processEvents()
            self.assertFalse(window.asr_context_edit.isEnabled())
            self.assertFalse(window.asr_language_combo.isEnabled())
            self.assertEqual(window.settings.asr.context, "保留的 Qwen 热词")
            self.assertEqual(window.settings.asr.language, "Chinese")

            window.quality_model_combo.setCurrentIndex(
                window.quality_model_combo.findData("qwen3"))
            self.app.processEvents()
            self.assertTrue(window.asr_context_edit.isEnabled())
            self.assertTrue(window.asr_language_combo.isEnabled())
            window.close()
            window.deleteLater()


if QT_AVAILABLE:
    class _DelayedWorker(QObject):
        log = Signal(str)
        step = Signal(str)
        progress = Signal(float)
        finished = Signal(list)
        failed = Signal(str)
        cancelled = Signal()

        def __init__(self, _job):
            super().__init__()
            self._cancel_requested = False

        @property
        def is_cancelling(self):
            return self._cancel_requested

        def cancel(self):
            self._cancel_requested = True

        @Slot()
        def run(self):
            QTimer.singleShot(40, self._complete)

        @Slot()
        def _complete(self):
            if self._cancel_requested:
                self.cancelled.emit()
            else:
                self.finished.emit([])


if __name__ == "__main__":
    unittest.main()
