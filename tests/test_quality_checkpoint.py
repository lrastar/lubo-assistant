import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from lubo.quality_checkpoint import (BusyQualityJob, JobLock, RecordingCheckpoint,
                                     recording_fingerprint, sha256_file, source_signature)


class RecordingCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"model": {"revision": "rev-a"}}
        self.parameters = {"language": "Chinese", "placement": "cpu"}
        self.fingerprint, self.canonical_identity = recording_fingerprint(
            "audio-hash", self.identity, self.parameters)

    def test_fingerprint_changes_with_audio_model_and_effective_parameters(self):
        baseline = self.fingerprint
        self.assertNotEqual(recording_fingerprint("other-audio", self.identity, self.parameters)[0], baseline)
        self.assertNotEqual(recording_fingerprint("audio-hash", {"model": {"revision": "rev-b"}},
                                                  self.parameters)[0], baseline)
        self.assertNotEqual(recording_fingerprint("audio-hash", self.identity,
                                                  {"language": "English", "placement": "cpu"})[0], baseline)

    def test_chunk_and_each_stage_resume_after_checkpoint_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = RecordingCheckpoint(root, self.fingerprint, self.canonical_identity)
            chunk = checkpoint.chunk_path("chunk-00000")
            chunk.write_bytes(b"wave bytes")
            checkpoint.remember_chunk("chunk-00000", sha256_file(chunk), 1.0)
            checkpoint.commit_stage("recognition", "chunk-00000", {"text": "你好"})
            checkpoint.commit_stage("alignment", "chunk-00000", {"cues": [{"text": "你好"}]})

            resumed = RecordingCheckpoint(root, self.fingerprint, self.canonical_identity)
            self.assertEqual(resumed.verified_chunk("chunk-00000"), chunk)
            self.assertEqual(resumed.read_stage("recognition", "chunk-00000"), {"text": "你好"})
            self.assertEqual(resumed.read_stage("alignment", "chunk-00000"), {"cues": [{"text": "你好"}]})
            chunk.write_bytes(b"changed")
            self.assertIsNone(resumed.verified_chunk("chunk-00000"))

    def test_non_object_manifest_and_stage_json_are_ignored_with_diagnostics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text("[]", encoding="utf-8")
            checkpoint = RecordingCheckpoint(root, self.fingerprint, self.canonical_identity)
            self.assertTrue(any("顶层不是对象" in warning for warning in checkpoint.cache_warnings))
            stage_path = checkpoint.stage_path("recognition", "chunk-00000")
            stage_path.write_text("[]", encoding="utf-8")
            self.assertIsNone(checkpoint.read_stage("recognition", "chunk-00000"))
            self.assertTrue(any("阶段缓存顶层无效" in warning for warning in checkpoint.cache_warnings))

    def test_source_change_invalidates_all_derived_cache_for_the_old_fingerprint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.wav"
            source.write_bytes(b"original source")
            checkpoint_root = root / "checkpoint"
            checkpoint = RecordingCheckpoint(
                checkpoint_root, self.fingerprint, self.canonical_identity,
                source_path=source, expected_source_signature=source_signature(source))
            chunk = checkpoint.chunk_path("chunk-00000")
            chunk.write_bytes(b"chunk from original source")
            checkpoint.remember_chunk("chunk-00000", sha256_file(chunk), 1.0)
            checkpoint.commit_stage("recognition", "chunk-00000", {"text": "旧音频"})
            checkpoint.commit_stage("alignment", "chunk-00000", {"words": []})

            source.write_bytes(b"changed source with different length")
            with self.assertRaisesRegex(RuntimeError, "源音频在识别期间发生变化"):
                checkpoint.commit_stage("recognition", "chunk-00001", {"text": "混合音频"})

            resumed = RecordingCheckpoint(checkpoint_root, self.fingerprint, self.canonical_identity)
            self.assertIsNone(resumed.verified_chunk("chunk-00000"))
            self.assertIsNone(resumed.read_stage("recognition", "chunk-00000"))
            self.assertIsNone(resumed.read_stage("alignment", "chunk-00000"))
            self.assertEqual(resumed.manifest["chunks"], {})
            self.assertEqual(resumed.manifest["stages"], {"recognition": {}, "alignment": {}})
            self.assertFalse(chunk.exists())

    def test_job_lock_reports_busy_and_process_death_releases_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "recording.lock"
            ready_path = root / "ready"
            script = (
                "from pathlib import Path; import sys,time; "
                "from lubo.quality_checkpoint import JobLock; "
                "lock=JobLock(Path(sys.argv[1])); lock.acquire(); "
                "Path(sys.argv[2]).write_text('ready'); time.sleep(120)"
            )
            child = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(ready_path)],
                                     cwd=Path(__file__).resolve().parents[1],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(ready_path.exists(), "child process did not acquire the job lock")
                with self.assertRaises(BusyQualityJob):
                    JobLock(lock_path).acquire()
                child.kill()
                child.wait(timeout=5)
                time.sleep(.1)  # Wait for the OS to finish closing the terminated process handles.
                with JobLock(lock_path):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
