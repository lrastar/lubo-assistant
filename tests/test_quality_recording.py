import unittest

from lubo.quality_audio import AudioWindow
from lubo.quality_recording import stitch_windows


def segment(text, times):
    return {"text": text, "words": [{"text": ch, "start_time": a, "end_time": b}
                                     for ch, (a, b) in zip(text, times)]}


class RecordingAssemblyTests(unittest.TestCase):
    def test_padding_is_not_counted_as_extra_audio_or_duplicate_text(self):
        windows = [AudioWindow(0, 0, 2000, 0, 2500), AudioWindow(1, 2000, 4000, 1500, 4000)]
        first = segment("你好再见", [(0, .2), (.2, .4), (2.1, 2.3), (2.3, 2.5)])
        second = segment("好再见", [(0, .1), (.6, .8), (.8, 1)])
        cues = stitch_windows(windows, [first, second], 4, 22)
        self.assertEqual([c.text for c in cues], ["你好", "再见"])
        self.assertAlmostEqual(cues[1].start, 2.1)
        self.assertAlmostEqual(cues[1].end, 2.5)

    def test_absolute_time_after_two_hours_does_not_accumulate_chunk_padding(self):
        windows = [AudioWindow(i, i * 120000, i * 120000 + 2000,
                               i * 120000, i * 120000 + 2000) for i in range(61)]
        cues = stitch_windows(windows, [segment("测试", [(.2, .4), (.4, .6)]) for _ in windows], 7202, 22)
        self.assertEqual(len(cues), 61)
        self.assertAlmostEqual(cues[-1].start, 7200.2)
        self.assertAlmostEqual(cues[-1].end, 7200.6)

    def test_missing_or_failed_chunks_cannot_be_delivered_as_success(self):
        windows = [AudioWindow(0, 0, 2000, 0, 2000)]
        with self.assertRaises(ValueError):
            stitch_windows(windows, [], 2, 22)
        with self.assertRaises(RuntimeError):
            stitch_windows(windows, [{"error": "failed"}], 2, 22)

    def test_invalid_timestamps_are_not_replaced_with_uniform_guesses(self):
        window = AudioWindow(0, 0, 2000, 0, 2000)
        with self.assertRaises(ValueError):
            stitch_windows([window], [segment("测试", [(1, 1.2), (.3, .5)])], 2, 22)

    def test_zero_duration_final_word_is_owned_by_last_window(self):
        window = AudioWindow(0, 0, 2000, 0, 2000)
        self.assertTrue(window.owns_word(2, 2))


if __name__ == "__main__":
    unittest.main()
