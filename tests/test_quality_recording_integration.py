from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import patch

from lubo.asr_backend import effective_parameters, model_identity
from lubo.asr_profiles import get_asr_profile
from lubo.quality_audio import plan_windows, write_window
from lubo.quality_checkpoint import (RecordingCheckpoint, recording_fingerprint,
                                     sha256_file, source_signature)
from lubo.quality_recording import run_recording
from lubo.quality_worker import run_job
from lubo.wav_audio import WavReader


def make_silence(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * (16000 * seconds))


class RecordingRecoveryIntegrationTests(unittest.TestCase):
    def test_recording_resume_runs_only_missing_recognition_and_alignment_chunks(self):
        class FakeAdapter:
            def __init__(self):
                self.torch = SimpleNamespace(__version__="stub")
                self.transformers_version = "stub"
                self.model_info = {"revision": "test"}
                self.recognized = []

            def load(self, _job, _log):
                pass

            def transcribe(self, sample):
                self.recognized.append(sample["id"])
                return {"text": "你好"}

            def close(self):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.wav"
            make_silence(source, 49)
            windows = plan_windows(source)
            self.assertEqual(len(windows), 2)

            profile = get_asr_profile("qwen3")
            identity = model_identity(profile, root / "unused-models")
            parameters = effective_parameters(profile, SimpleNamespace(context="", language=""), 22)
            fingerprint, job_identity = recording_fingerprint(sha256_file(source), identity, parameters)
            job_root = root / fingerprint[:2] / fingerprint
            log_path = root / "runs" / "attempt" / "worker.log"
            log_path.parent.mkdir(parents=True)
            progress_path = log_path.with_name("progress.json")
            checkpoint = RecordingCheckpoint(
                job_root, fingerprint, job_identity,
                source_path=source, expected_source_signature=source_signature(source))
            first = windows[0]
            first_id = "chunk-00000"
            first_chunk = checkpoint.chunk_path(first_id)
            with WavReader(source) as reader:
                write_window(reader, first, first_chunk)
            checkpoint.remember_chunk(first_id, sha256_file(first_chunk),
                                      (first.read_end_ms - first.read_start_ms) / 1000)
            checkpoint.commit_stage("recognition", first_id, {
                "text": "你好", "language": None, "native_words": [], "confidence": None,
            })
            first_words = [
                {"text": "你", "start_time": 1.0, "end_time": 1.1},
                {"text": "好", "start_time": 1.1, "end_time": 1.2},
            ]
            checkpoint.commit_stage("alignment", first_id, {"words": first_words, "cues": []})

            adapter = FakeAdapter()
            aligned = []

            def align_missing(_job, report, _log, checkpoint=None):
                for item in report["samples"]:
                    cached = checkpoint.read_stage("alignment", item["id"])
                    if cached is not None:
                        item.update(cached)
                        continue
                    aligned.append(item["id"])
                    offset = 3.0 if item["id"] == "chunk-00001" else 1.0
                    words = [
                        {"text": "你", "start_time": offset, "end_time": offset + .1},
                        {"text": "好", "start_time": offset + .1, "end_time": offset + .2},
                    ]
                    result = {"words": words, "cues": []}
                    item.update(result)
                    checkpoint.commit_stage("alignment", item["id"], result)

            job = {
                "model_key": profile.key,
                "model_identity": identity,
                "effective_parameters": parameters,
                "source_sha256": sha256_file(source),
                "source_signature": source_signature(source),
                "wav_path": str(source),
                "job_fingerprint": fingerprint,
                "job_identity": job_identity,
                "job_dir": str(job_root),
                "progress_path": str(progress_path),
                "log_path": str(log_path),
                "model_root": str(root / "quality-models"),
                "max_chars": 22,
            }
            with patch("lubo.quality_models.download_model"), \
                    patch("lubo.quality_worker.create_quality_asr_adapter", return_value=adapter), \
                    patch("lubo.quality_worker.align_results", side_effect=align_missing):
                result = run_recording(job, root / "result.json")

            self.assertTrue(result["complete"] and result["ok"])
            self.assertEqual(adapter.recognized, ["chunk-00001"])
            self.assertEqual(aligned, ["chunk-00001"])
            self.assertTrue(result["cues"])
            missing_chunk = checkpoint.chunk_path("chunk-00001")
            self.assertEqual(checkpoint.read_stage("recognition", "chunk-00001")["audio_sha256"],
                             sha256_file(missing_chunk))

    def test_failed_chunk_keeps_error_record_and_never_creates_success_stage(self):
        class FailedAdapter:
            torch = SimpleNamespace(__version__="stub")
            transformers_version = "stub"
            model_info = {"revision": "test"}

            def load(self, _job, _log):
                pass

            def transcribe(self, _sample):
                raise RuntimeError("decoder failed after partial output")

            def close(self):
                pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "chunk.wav"
            make_silence(audio, 1)
            fingerprint, identity = recording_fingerprint("audio", {"model": "qwen3"}, {})
            checkpoint = RecordingCheckpoint(root / "checkpoint", fingerprint, identity)
            job = {"engine": "qwen3", "samples": [{"id": "chunk-00000", "audio": str(audio)}],
                   "log_path": str(root / "worker.log")}
            report_path = root / "report.json"
            with patch("lubo.quality_worker.create_quality_asr_adapter", return_value=FailedAdapter()):
                report = run_job(job, report_path, checkpoint=checkpoint)

            self.assertFalse(report["ok"])
            self.assertIn("decoder failed", report["samples"][0]["error"])
            self.assertIsNone(checkpoint.read_stage("recognition", "chunk-00000"))
            failure_path = checkpoint.failures_dir / "recognition-chunk-00000.json"
            self.assertTrue(failure_path.is_file())
            self.assertIn("decoder failed", failure_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
