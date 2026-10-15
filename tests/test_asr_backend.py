import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from lubo.asr import Transcriber
from lubo.asr_backend import (QualityTranscriber, _terminate_process_tree, create_transcriber,
                              firered_code_root, firered_model_root, firered_python_path)
from lubo.config import AsrOptions
from lubo.ffmpeg import Cancelled


class BackendSelectionTests(unittest.TestCase):
    def test_default_uses_quality_and_original_backend_remains_available(self):
        self.assertIsInstance(create_transcriber(AsrOptions()), QualityTranscriber)
        quick = create_transcriber(AsrOptions(engine='paraformer'))
        self.assertIsInstance(quick, QualityTranscriber)
        self.assertEqual(quick.engine, "paraformer")
        self.assertTrue(callable(Transcriber.transcribe), "direct class stays available for legacy callers")

    def test_existing_positional_device_argument_keeps_its_meaning(self):
        options = AsrOptions('cpu')
        self.assertEqual(options.device, 'cpu')
        self.assertEqual(options.engine, 'quality')

    def test_unknown_engine_fails_clearly(self):
        with self.assertRaises(ValueError):
            create_transcriber(AsrOptions(engine='unknown'))

    def test_firered_runtime_root_override_and_specific_overrides(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "shared-runtime"
            with patch.dict(os.environ, {"LUBO_ASR_RUNTIMES": str(runtime)}, clear=True):
                self.assertEqual(firered_model_root(), runtime / "fireredasr2-aed")
                self.assertEqual(firered_code_root(), runtime / "FireRedASR2S")
                self.assertEqual(firered_python_path(), runtime / "firered-venv" / "Scripts" / "python.exe")

            model, code, python = root / "custom-model", root / "custom-code", root / "custom-python.exe"
            with patch.dict(os.environ, {
                "LUBO_ASR_RUNTIMES": str(runtime),
                "LUBO_FIRERED_MODEL_DIR": str(model),
                "LUBO_FIRERED_CODE_DIR": str(code),
                "LUBO_FIRERED_PYTHON": str(python),
            }, clear=True):
                self.assertEqual(firered_model_root(), model)
                self.assertEqual(firered_code_root(), code)
                self.assertEqual(firered_python_path(), python)

    def test_taskkill_failure_still_falls_back_to_terminating_worker(self):
        for failure in (OSError("taskkill unavailable"), subprocess.TimeoutExpired("taskkill", 1)):
            with self.subTest(failure=type(failure).__name__):
                process = Mock()
                process.pid = 1234
                process.poll.return_value = None
                with patch("lubo.asr_backend.os.name", "nt"), \
                        patch("lubo.asr_backend.subprocess.run", side_effect=failure):
                    _terminate_process_tree(process, grace_seconds=.01)
                process.terminate.assert_called_once()
                process.wait.assert_called_once()

    def test_real_worker_process_receives_cancel_request_and_is_reaped(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "fake_worker.py"
            stopped = root / "stopped"
            worker.write_text(
                "import argparse,json,pathlib,time\n"
                "p=argparse.ArgumentParser();p.add_argument('--job');p.add_argument('--result');a=p.parse_args()\n"
                "j=json.loads(pathlib.Path(a.job).read_text())\n"
                "while not pathlib.Path(j['cancel_path']).exists(): time.sleep(.01)\n"
                + f"pathlib.Path({str(stopped)!r}).write_text('cancelled')\n"
                + "pathlib.Path(a.result).write_text(json.dumps({'complete':False,'ok':False}))\n",
                encoding="utf-8",
            )
            job_path, result_path = root / "job.json", root / "result.json"
            cancel_path = root / "cancel.requested"
            job = {"cancel_path": str(cancel_path)}
            job_path.write_text(json.dumps(job), encoding="utf-8")
            cancel = threading.Event()
            timer = threading.Timer(.3, cancel.set)
            timer.start()
            transcriber = QualityTranscriber(AsrOptions(engine="paraformer"), engine="paraformer")
            command = [sys.executable, str(worker), "--job", str(job_path), "--result", str(result_path)]
            with patch.object(transcriber, "_command", return_value=command), \
                    patch("lubo.asr_backend.application_directory", return_value=root):
                with self.assertRaises(Cancelled):
                    transcriber._run_worker(job, job_path, result_path, root / "progress.json",
                                            None, cancel, root / "worker.log", "快捷识别")
            timer.join(timeout=2)
            self.assertTrue(stopped.is_file(), "child did not observe the cancel file")
            self.assertIsNone(transcriber._process)
            self.assertTrue(cancel_path.is_file())


if __name__ == '__main__':
    unittest.main()
