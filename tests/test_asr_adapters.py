import importlib.util
from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import Mock, patch

import numpy as np

from lubo.asr_adapters import FireRedAsr2Adapter, Qwen3Adapter
from lubo.asr_backend import model_identity
from lubo.asr_profiles import get_asr_profile
from lubo.quality_checkpoint import RecordingCheckpoint, recording_fingerprint, sha256_file
from lubo.quality_worker import run_job


def make_wave(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * (16000 * seconds))


class QwenLanguageTests(unittest.TestCase):
    def test_auto_detection_reaches_processor_as_none_and_worker_completes(self):
        # The installed processor accepts None for auto-detection and rejects ''.
        class Inputs(dict):
            def to(self, *_args):
                return self

        for job_language, sample_language, expected in [
            ("", {}, None),
            ("", {"language": ""}, None),
            ("Chinese", {"language": ""}, None),
            (None, {}, None),
            ("Chinese", {}, "Chinese"),
            ("Chinese", {"language": "English"}, "English"),
        ]:
            with self.subTest(job_language=job_language, sample=sample_language), TemporaryDirectory() as directory:
                root = Path(directory)
                audio = root / "sample.wav"
                make_wave(audio)
                processor = Mock()

                def prepare(**kwargs):
                    if kwargs["language"] == "":
                        raise ValueError("Unsupported language: ''")
                    return Inputs(input_ids=np.array([[1, 2]]))

                processor.apply_transcription_request.side_effect = prepare
                processor.decode.return_value = [{"transcription": "你好", "language": "Chinese"}]
                adapter = Qwen3Adapter()
                adapter.processor = processor
                adapter.model = SimpleNamespace(device="cpu", dtype="float32",
                                                generate=lambda **_kwargs: np.array([[1, 2, 3]]))
                adapter.torch = SimpleNamespace(__version__="stub", inference_mode=nullcontext)
                adapter.transformers_version = "stub"
                adapter.model_info = {"revision": "stub"}
                job = {"engine": "qwen3", "language": job_language, "align": False,
                       "samples": [{"id": "chunk-00000", "audio": str(audio), **sample_language}]}
                adapter.job = job
                with patch.object(adapter, "load"), patch.object(adapter, "close"), \
                        patch("lubo.quality_worker.create_quality_asr_adapter", return_value=adapter):
                    result = run_job(job, root / "result.json")
                self.assertTrue(result["ok"], result["samples"])
                self.assertEqual(result["samples"][0]["text"], "你好")
                self.assertEqual(processor.apply_transcription_request.call_args.kwargs["language"], expected)


class FireRedAdapterTests(unittest.TestCase):
    def test_worker_installer_and_registry_pin_the_same_revisions(self):
        runtime_path = Path(__file__).resolve().parents[1] / "scripts" / "firered_runtime.py"
        spec = importlib.util.spec_from_file_location("test_firered_runtime", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        profile = get_asr_profile("firered_asr2_aed")
        self.assertEqual(profile.asr_revision, runtime.MODEL_REVISION)
        self.assertEqual(profile.code_revision, runtime.CODE_REVISION)
        self.assertEqual(profile.code_repo, runtime.CODE_REPO)
        self.assertEqual(len(profile.code_revision), 40)
        setup_script = runtime_path.with_name("setup_firered.ps1").read_text(encoding="utf-8")
        self.assertIn(f'$codeRevision = "{profile.code_revision}"', setup_script)

    def test_installer_marker_is_accepted_by_parent_recording_identity(self):
        runtime_path = Path(__file__).resolve().parents[1] / "scripts" / "firered_runtime.py"
        spec = importlib.util.spec_from_file_location("test_firered_marker_runtime", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = get_asr_profile("firered_asr2_aed")
            marker = {"repo_id": runtime.MODEL_REPO, "revision": runtime.MODEL_REVISION,
                      "code_repo": runtime.CODE_REPO, "code_revision": runtime.CODE_REVISION,
                      "verified": True, "files": {"model.pth.tar": {
                          "size": profile.asr_model_size, "sha256": profile.asr_model_sha256}}}
            path = root / "lubo-model.json"
            path.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(model_identity(profile, root)["asr"]["code_repo"], runtime.CODE_REPO)
            marker["code_repo"] = "https://github.com/unrelated/source.git"
            path.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                model_identity(profile, root)

    def test_incremental_report_maps_to_shared_asr_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            make_wave(audio)
            profile = get_asr_profile("firered_asr2_aed")
            report = root / "report.json"
            report.write_text(json.dumps({
                "model_revision": profile.asr_revision,
                "code_revision": profile.code_revision,
                "samples": [{"id": "chunk-00000", "audio": str(audio),
                             "audio_sha256": sha256_file(audio), "duration_s": 1.0, "ok": True,
                             "text": "你好", "words": [], "alignment_ok": False,
                             "elapsed_s": .25}],
            }), encoding="utf-8")
            adapter = FireRedAsr2Adapter()
            results = []
            adapter._consume_report(
                report, {"chunk-00000": {"id": "chunk-00000", "audio": str(audio)}},
                set(), lambda source, result: results.append((source, result)))
            self.assertEqual(len(results), 1)
            source, result = results[0]
            self.assertEqual(source["id"], result["id"])
            self.assertEqual(result["text"], "你好")
            self.assertEqual(result["audio_sha256"], sha256_file(audio))
            self.assertEqual(result["duration"], 1.0)
            self.assertEqual(result["native_words"], [])
            self.assertFalse(result["native_alignment_valid"])
            self.assertNotIn("error", result)

    def test_wrong_input_audio_hash_is_rejected_before_checkpoint_callback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            make_wave(audio)
            profile = get_asr_profile("firered_asr2_aed")
            report = root / "report.json"
            report.write_text(json.dumps({
                "model_revision": profile.asr_revision,
                "code_revision": profile.code_revision,
                "samples": [{"id": "chunk-00000", "audio": str(audio),
                             "audio_sha256": "wrong", "duration_s": 1.0, "ok": True,
                             "text": "你好"}],
            }), encoding="utf-8")
            committed = []
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                FireRedAsr2Adapter()._consume_report(
                    report, {"chunk-00000": {"id": "chunk-00000", "audio": str(audio)}}, set(),
                    lambda *_: committed.append(True))
            self.assertEqual(committed, [])

    def test_final_report_must_prove_complete_success(self):
        profile = get_asr_profile("firered_asr2_aed")
        pending = {"chunk-00000": {"audio": "sample.wav"}}
        report = {"model_revision": profile.asr_revision, "code_revision": profile.code_revision,
                  "status": "partial", "complete": False, "ok": False,
                  "completed_count": 1, "sample_count": 1,
                  "samples": [{"id": "chunk-00000", "ok": True}]}
        with self.assertRaisesRegex(RuntimeError, "终态报告"):
            FireRedAsr2Adapter._validate_final_report(report, profile, pending)

    def test_sequential_jobs_in_same_log_directory_never_consume_stale_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            make_wave(audio)
            profile = get_asr_profile("firered_asr2_aed")

            def make_report(text: str) -> dict:
                return {
                    "model_revision": profile.asr_revision,
                    "code_revision": profile.code_revision,
                    "status": "completed",
                    "complete": True,
                    "ok": True,
                    "completed_count": 1,
                    "sample_count": 1,
                    "samples": [{
                        "id": "chunk-00000", "audio": str(audio),
                        "audio_sha256": sha256_file(audio), "duration_s": 1.0,
                        "ok": True, "text": text, "words": [], "alignment_ok": False,
                        "elapsed_s": .01,
                    }],
                }

            # Simulate a valid report left by a preceding run with the same sample id.
            (root / "firered-report.json").write_text(
                json.dumps(make_report("stale result")), encoding="utf-8")
            run_dirs: list[Path] = []
            transcript = {"text": "current result"}

            class FakeProcess:
                returncode = 0

                def __init__(self, request_path: Path):
                    self.request = json.loads(request_path.read_text(encoding="utf-8"))
                    self.report_path = Path(self.request["report_path"])
                    run_dirs.append(self.report_path.parent)
                    self.poll_count = 0

                def poll(self):
                    self.poll_count += 1
                    if self.poll_count == 1:
                        return None
                    self.report_path.write_text(json.dumps(make_report(transcript["text"])),
                                                encoding="utf-8")
                    return 0

            def fake_popen(command, **_kwargs):
                request_path = Path(command[command.index("--job") + 1])
                return FakeProcess(request_path)

            adapter = FireRedAsr2Adapter()
            adapter.job = {"log_path": str(root / "worker.log")}
            adapter.python = root / "python.exe"
            adapter.worker = root / "firered_worker.py"
            adapter.model_root = root / "model"
            adapter.code_root = root / "code"
            adapter.log = lambda _message: None
            sample = {"id": "chunk-00000", "audio": str(audio)}

            with patch("lubo.asr_adapters.subprocess.Popen", side_effect=fake_popen):
                for expected_text in ("first current result", "second current result"):
                    transcript["text"] = expected_text
                    results = []
                    adapter.transcribe_batch([sample], lambda _source, result: results.append(result))
                    self.assertEqual([result["text"] for result in results], [expected_text])

            self.assertEqual(len(run_dirs), 2)
            self.assertNotEqual(run_dirs[0], run_dirs[1])
            self.assertTrue(all(path.parent.name == "firered-runs" for path in run_dirs))
            self.assertTrue((root / "firered-report.json").is_file())

    def test_wrong_revision_is_rejected_before_checkpoint_callback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            make_wave(audio)
            report = root / "report.json"
            report.write_text(json.dumps({
                "model_revision": "wrong", "code_revision": "wrong",
                "samples": [{"id": "chunk-00000", "ok": True, "text": "你好"}],
            }), encoding="utf-8")
            committed = []
            with self.assertRaisesRegex(RuntimeError, "revision"):
                FireRedAsr2Adapter()._consume_report(
                    report, {"chunk-00000": {"id": "chunk-00000", "audio": str(audio)}}, set(),
                    lambda *_: committed.append(True))
            self.assertEqual(committed, [])

    def test_asr_success_without_native_timing_remains_alignable(self):
        class FakeFireRed:
            def load(self, _job, _log):
                pass

            def transcribe_batch(self, samples, on_result):
                sample = samples[0]
                on_result(sample, {"id": sample["id"], "audio": sample["audio"],
                                   "duration": 1.0, "text": "你好", "native_words": [],
                                   "elapsed_seconds": .1})

            def close(self):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            make_wave(audio)
            fingerprint, identity = recording_fingerprint("audio", {"model": "fire-red"}, {})
            checkpoint = RecordingCheckpoint(root / "job", fingerprint, identity)
            report_path = root / "report.json"
            job = {"engine": "firered_asr2_aed", "samples": [{"id": "chunk-00000",
                                                                  "audio": str(audio)}],
                   "align": False, "log_path": str(root / "worker.log")}
            with patch("lubo.quality_worker.create_quality_asr_adapter", return_value=FakeFireRed()):
                report = run_job(job, report_path, checkpoint=checkpoint)
            self.assertTrue(report["ok"])
            sample = report["samples"][0]
            self.assertEqual(sample["text"], "你好")
            self.assertNotIn("error", sample)
            self.assertFalse(sample["native_alignment_valid"])
            cached = checkpoint.read_stage("recognition", "chunk-00000")
            self.assertEqual(cached["text"], "你好")
            self.assertFalse(cached["native_alignment_valid"])


if __name__ == "__main__":
    unittest.main()
