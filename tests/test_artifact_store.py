import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from lubo.artifact_store import ArtifactStore, write_json_atomic
from lubo.config import Settings
from lubo.pipeline import Job, Pipeline, PipelineCallbacks
from lubo.segments import Segment


def make_job(folder: Path, *, model: str = "qwen3", reuse: bool = False,
             strict: bool = False) -> tuple[Job, Path, Path]:
    video = folder / "source.mp4"
    video.write_bytes(b"source-video")
    srt = folder / "output" / "source.srt"
    srt.parent.mkdir(parents=True, exist_ok=True)
    segment = Segment(video, None, None, None, video.stat().st_size)
    settings = Settings()
    settings.asr.quality_model = model
    settings.asr.reuse_existing_srt = reuse
    job = Job([segment], settings, srt.parent, "source", strict_srt_source=strict)
    return job, video, srt


class ArtifactStoreTests(unittest.TestCase):
    def test_asr_fingerprint_invalidates_unedited_srt_but_manual_edit_is_recognized(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            job, video, srt = make_job(root)
            store = ArtifactStore(job, work_root=root / "work", state_root=root / "state")
            store.capture_sources()
            srt.write_text("generated subtitle", encoding="utf-8")
            store.save_srt_record(srt)

            self.assertEqual(store.srt_reuse_state(srt), "match")
            job.settings.asr.quality_model = "firered_asr2_aed"
            self.assertEqual(store.srt_reuse_state(srt), "stale")

            srt.write_text("user corrected subtitle", encoding="utf-8")
            self.assertEqual(store.srt_source_state(srt), "match")
            self.assertEqual(store.srt_reuse_state(srt), "edited")

            video.write_bytes(b"source-video-changed")
            changed_source = ArtifactStore(job, work_root=root / "work", state_root=root / "state")
            changed_source.capture_sources()
            self.assertEqual(changed_source.srt_source_state(srt), "mismatch")
            self.assertEqual(changed_source.srt_reuse_state(srt), "mismatch")

    def test_legacy_source_record_and_user_srt_survive_model_change_when_reuse_is_explicit(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            job, _, srt = make_job(root, model="firered_asr2_aed", reuse=True, strict=True)
            store = ArtifactStore(job, work_root=root / "work", state_root=root / "state")
            store.capture_sources()
            manual_text = "字幕已由用户校对"
            srt.write_text(manual_text, encoding="utf-8")
            legacy_record = {
                "version": 1,
                "srt": str(srt.resolve()),
                "segments": store.source_snapshot,
            }
            record_path = store.job_record_path("srt", srt)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(legacy_record, ensure_ascii=False), encoding="utf-8")

            messages: list[str] = []
            pipeline = Pipeline(
                job,
                PipelineCallbacks(messages.append, messages.append, lambda _: None),
                work_root=root / "work",
                state_root=root / "state",
            )
            pipeline.artifacts.capture_sources()
            result = pipeline._srt(job.segments[0].video, srt, 10.0, True, 1.0)

            self.assertEqual(result, srt)
            self.assertEqual(srt.read_text(encoding="utf-8"), manual_text)
            self.assertTrue(any("按设置保留" in message for message in messages))
            self.assertEqual(pipeline.artifacts.srt_reuse_state(srt), "legacy")

    def test_cleanup_unlinks_only_registered_paths_inside_job_work_directory(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            job, original, _ = make_job(root)
            final = job.output_dir / "source.完整版.mp4"
            final.write_bytes(b"user-visible output")
            store = ArtifactStore(job, work_root=root / "work", state_root=root / "state")
            store.work_dir.mkdir(parents=True)
            intermediate = store.work_dir / "temporary.wav"
            intermediate.write_bytes(b"generated intermediate")
            messages: list[str] = []

            store.cleanup({intermediate, original, final}, [final], False, messages.append)

            self.assertFalse(intermediate.exists())
            self.assertTrue(original.exists())
            self.assertTrue(final.exists())
            self.assertTrue(any("工作目录外" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
