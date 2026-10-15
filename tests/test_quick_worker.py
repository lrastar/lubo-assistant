import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from lubo.ffmpeg import Cancelled
from lubo.quality_worker import publish_progress, run_quick_job


class QuickWorkerCancellationTests(unittest.TestCase):
    def test_worker_progress_never_moves_backwards_between_stages(self):
        with TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"
            observed = []
            for value, message in ((.075, "aligner download"), (.02, "ASR download"),
                                   (.06, "ASR model load"), (.74, "aligner load"),
                                   (.99, "align complete")):
                publish_progress(progress, value, message)
                report = json.loads(progress.read_text(encoding="utf-8"))
                observed.append(report["progress"])
            self.assertEqual(observed, [.075, .075, .075, .74, .99])
            self.assertEqual(report["message"], "align complete")

    def test_cancel_file_reaches_paraformer_transcriber_and_prevents_success_report(self):
        entered_inference = threading.Event()
        caught = []

        class BlockingTranscriber:
            def __init__(self, _options, log=None):
                self.log = log

            def transcribe(self, _wav_path, max_chars, on_progress, cancel):
                self.assertions = max_chars == 22 and callable(on_progress)
                entered_inference.set()
                while not cancel.wait(.01):
                    pass
                raise Cancelled()

            def close(self):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            cancel_path = root / "cancel.requested"
            progress_path = root / "progress.json"
            result_path = root / "result.json"
            job = {"wav_path": str(root / "audio.wav"), "max_chars": 22,
                   "progress_path": str(progress_path), "cancel_path": str(cancel_path)}

            def run():
                try:
                    run_quick_job(job, result_path)
                except BaseException as exc:
                    caught.append(exc)

            with patch("lubo.asr.Transcriber", BlockingTranscriber):
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(entered_inference.wait(3), "worker never started transcription")
                cancel_path.write_text("cancel\n", encoding="ascii")
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive(), "worker did not finish after cancellation")
            self.assertEqual(len(caught), 1)
            self.assertIsInstance(caught[0], Cancelled)
            report = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertFalse(report["complete"])
            self.assertFalse(report["ok"])
            self.assertEqual(report["cues"], [])


if __name__ == "__main__":
    unittest.main()
