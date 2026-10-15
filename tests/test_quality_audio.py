from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

import numpy as np

from lubo.asr import WavReader
from lubo.quality_audio import AudioWindow, plan_windows, write_window


class AudioWindowTests(unittest.TestCase):
    def make_wave(self, directory, samples, rate=16000):
        path = Path(directory) / "input.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples.astype("<i2").tobytes())
        return path

    def test_cuts_use_pauses_and_do_not_remove_speech_or_silence(self):
        samples = np.full(16000 * 60, 2000, dtype=np.int16)
        samples[int(22.5 * 16000):23 * 16000] = 0
        samples[int(47.2 * 16000):int(47.6 * 16000)] = 0
        with TemporaryDirectory() as directory:
            windows = plan_windows(self.make_wave(directory, samples))
        self.assertTrue(22500 < windows[0].core_end_ms < 23000)
        self.assertEqual(windows[0].core_start_ms, 0)
        self.assertEqual(windows[-1].core_end_ms, 60000)
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous.core_end_ms, current.core_start_ms)
            self.assertGreater(previous.read_end_ms, current.read_start_ms)
        self.assertTrue(all(w.read_end_ms - w.read_start_ms <= 40000 for w in windows))

    def test_no_pause_falls_back_to_bounded_contiguous_windows(self):
        with TemporaryDirectory() as directory:
            windows = plan_windows(self.make_wave(directory, np.full(16000 * 61, 1000, dtype=np.int16)))
        self.assertEqual([(w.core_start_ms, w.core_end_ms) for w in windows],
                         [(0, 24000), (24000, 48000), (48000, 61000)])

    def test_context_does_not_move_word_times_or_duplicate_ownership(self):
        first = AudioWindow(0, 0, 24000, 0, 25600)
        second = AudioWindow(1, 24000, 48000, 22400, 49600)
        self.assertTrue(first.owns_word(23.7, 23.9))
        self.assertFalse(second.owns_word(1.3, 1.5))
        self.assertFalse(first.owns_word(24.1, 24.3))
        self.assertTrue(second.owns_word(1.7, 1.9))
        self.assertAlmostEqual(second.offset_seconds + 1.7, 24.1)

    def test_written_context_preserves_absolute_sample_positions(self):
        samples = np.arange(16000 * 4, dtype=np.int32).astype(np.int16)
        window = AudioWindow(0, 1000, 2500, 875, 2625)
        with TemporaryDirectory() as directory:
            source = self.make_wave(directory, samples)
            target = Path(directory) / "chunk.wav"
            with WavReader(source) as reader:
                write_window(reader, window, target)
            with wave.open(str(target), "rb") as output:
                actual = np.frombuffer(output.readframes(output.getnframes()), dtype="<i2")
            np.testing.assert_array_equal(actual, samples[14000:42000])

    def test_last_partial_millisecond_is_preserved(self):
        with TemporaryDirectory() as directory:
            windows = plan_windows(self.make_wave(directory, np.zeros(16001, dtype=np.int16)))
        self.assertEqual(windows, [AudioWindow(0, 0, 1001, 0, 1001)])


if __name__ == "__main__":
    unittest.main()
