import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
import wave
from unittest.mock import patch

import numpy as np

from lubo.config import Settings
from lubo.pipeline import Job, Pipeline, PipelineCallbacks

FFMPEG = os.environ.get("LUBO_TEST_FFMPEG") or shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "FFmpeg is required for media-clock regression tests")
class AudioClockTests(unittest.TestCase):
    def test_extraction_keeps_leading_delay_and_internal_timestamp_gaps(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "gaps.mkv", root / "recognition.wav"
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=size=16x16:rate=1:duration=12", "-f", "lavfi", "-i",
                            "sine=frequency=1000:sample_rate=16000:duration=7", "-af",
                            "asetpts=PTS+16000*(1+2*gte(T\\,2)+2*gte(T\\,4))",
                            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            settings = Settings()
            pipeline = Pipeline(Job([], settings, root, "test"), PipelineCallbacks(lambda _: None, lambda _: None, lambda _: None))
            pipeline.work_dir = root
            with patch("lubo.ffmpeg.find_tool", return_value=FFMPEG):
                pipeline._extract_wav(source, output, 12, 1)
            with wave.open(str(output), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
                samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            self.assertAlmostEqual(duration, 12, delta=.07)
            self.assertEqual(int(np.max(np.abs(samples[4 * 16000:4 * 16000 + 800]))), 0)
            self.assertGreater(int(np.max(np.abs(samples[6 * 16000:6 * 16000 + 800]))), 100)
            self.assertTrue(pipeline._reuse_wav(output, source))
            pipeline._work_record_path("wav", output).unlink()
            self.assertFalse(pipeline._reuse_wav(output, source), "Old audio without a clock record must be regenerated")


if __name__ == "__main__":
    unittest.main()
