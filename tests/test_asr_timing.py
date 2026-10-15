import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lubo.asr import Transcriber, finalize_cues, sentence_to_cues
from lubo.config import AsrOptions
from lubo.subtitles import Cue


class SubtitleTimingTests(unittest.TestCase):
    def test_long_clause_does_not_split_inside_common_chinese_words(self):
        text = "其实我们需要买摄像头"
        sentence = {"text": text, "timestamp": [[i * 100, (i + 1) * 100] for i in range(len(text))],
                    "start": 0, "end": len(text) * 100}
        cues = sentence_to_cues(sentence, 6)
        boundaries = []
        position = 0
        for cue in cues[:-1]:
            position += len(cue.text)
            boundaries.append(position)
        for word in ("其实", "我们", "需要", "摄像头"):
            start = text.index(word)
            self.assertFalse(any(start < b < start + len(word) for b in boundaries), (word, cues))
        self.assertEqual("".join(c.text for c in cues), text)

    def test_punctuation_separates_sentences_below_character_limit(self):
        sentence = {
            "text": "这句结束了。接着说下句。",
            "raw_text": "这 句 结 束 了 接 着 说 下 句",
            "timestamp": [[i * 100, (i + 1) * 100] for i in range(10)],
            "start": 0, "end": 1000,
        }
        cues = sentence_to_cues(sentence, 22)
        self.assertEqual([c.text for c in cues], ["这句结束了", "接着说下句"])
        self.assertEqual([(c.start, c.end) for c in cues], [(0, .5), (.5, 1)])

    def test_speech_pause_separates_model_sentence(self):
        sentence = {
            "text": "先说一句再说一句",
            "timestamp": [[i * 100, (i + 1) * 100] for i in range(4)]
                         + [[2000 + i * 100, 2100 + i * 100] for i in range(4)],
            "start": 0, "end": 2400,
        }
        cues = sentence_to_cues(sentence, 22)
        self.assertEqual([c.text for c in cues], ["先说一句", "再说一句"])
        self.assertEqual([(c.start, c.end) for c in cues], [(0, .4), (2, 2.4)])

    def test_punctuation_does_not_discard_word_timestamps(self):
        # Punctuation has no acoustic timestamp. Uneven speaking speed must
        # not be replaced with uniform timing across the whole sentence.
        sentence = {
            "text": "先说完，接着讲下去。",
            "timestamp": [[0, 100], [100, 200], [200, 300], [300, 400],
                          [400, 500], [500, 600], [600, 700], [700, 800]],
            "start": 0, "end": 4000,
        }
        cues = sentence_to_cues(sentence, 4, offset=120)
        self.assertEqual(cues[-1].end, 120.8)
        self.assertTrue(all(c.end <= 120.8 for c in cues))

    def test_finalize_does_not_join_complete_single_character_sentence(self):
        first = sentence_to_cues({"text": "好。", "start": 0, "end": 400,
                                  "timestamp": [[0, 400]]}, 22)
        following = sentence_to_cues({"text": "下一句开始", "start": 500, "end": 1500,
                                      "timestamp": [[500 + i * 200, 700 + i * 200]
                                                    for i in range(5)]}, 22)
        cues = finalize_cues(first + following, 2, 22)
        self.assertEqual([c.text for c in cues], ["好", "下一句开始"])

    def test_mixed_chinese_english_and_numbers_keep_text_and_timing(self):
        sentence = {
            "text": "使用 Python 3.10。继续测试！",
            "raw_text": "使 用 Py@@ thon 3.10 继 续 测 试",
            "timestamp": [[i * 200, (i + 1) * 200] for i in range(9)],
            "start": 0, "end": 5000,
        }
        cues = sentence_to_cues(sentence, 22)
        self.assertEqual([c.text for c in cues], ["使用Python 3.10", "继续测试！"])
        self.assertEqual([(c.start, c.end) for c in cues], [(0, 1), (1, 1.8)])

    def test_compact_raw_text_uses_acoustic_character_times(self):
        sentence = {"text": "字幕保持同步？", "raw_text": "字幕保持同步",
                    "timestamp": [[i * 100, (i + 1) * 100] for i in range(6)],
                    "start": 0, "end": 10000}
        cues = sentence_to_cues(sentence, 3)
        self.assertEqual("".join(c.text for c in cues), "字幕保持同步？")
        self.assertEqual(cues[-1].end, .6)
        self.assertTrue(all(len(c.text) <= 3 for c in cues))

    def test_missing_timestamps_are_confined_to_their_own_sentence(self):
        with self.assertLogs("lubo.asr_timing", level="WARNING"):
            cues = sentence_to_cues({"text": "没有时间戳，继续显示。",
                                     "start": 1000, "end": 3000}, 6, 120)
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0].start, cues[0].end), (121, 123))
        self.assertEqual(cues[0].text, "没有时间戳，继续显示")

    def test_overlap_cleanup_does_not_push_later_cues(self):
        cues = [Cue(0, 1, "重复起点"), Cue(0, 2, "重复起点"),
                Cue(.8, 1.3, "下一句"), Cue(1.3, 2, "再下一句")]
        result = finalize_cues(cues, 2)
        self.assertEqual([(c.start, c.end) for c in result], [(0, .8), (.8, 1.3), (1.3, 2)])
        self.assertEqual(cues[0].end, 1, "Cleanup must not mutate its input")

    def test_zero_duration_interjection_does_not_hide_the_following_sentence(self):
        cues = [Cue(1, 1, "哎"), Cue(1, 3, "我那个头像不会就是这个人吧？"), Cue(4, 5, "下一句")]
        result = finalize_cues(cues, 6)
        self.assertEqual(result[0].start, 1)
        self.assertEqual(result[0].end, 3)
        self.assertIn("我那个头像不会就是这个人吧？", result[0].text)
        self.assertTrue(result[0].text.startswith("哎"))
        self.assertEqual(result[1].start, 4)

    def test_same_time_repeated_interjections_are_not_assumed_duplicates(self):
        result = finalize_cues([Cue(1, 1, "嗯"), Cue(1, 1.2, "嗯"), Cue(2, 3, "继续说")], 4)
        self.assertEqual(result[0].text, "嗯，嗯")
        self.assertEqual(result[0].start, 1)
        self.assertEqual(result[1].start, 2)

    def test_two_hours_of_chunks_keep_absolute_speech_times(self):
        import numpy as np

        # Replay model output through transcribe -> _recognize -> sentence
        # splitting -> finalization. The reader supplies bounded audio blocks;
        # this exercises global offsets without allocating two hours of PCM.
        segments = [[i * 120000 + 1000, i * 120000 + 2000] for i in range(60)]
        reader = SimpleNamespace(total_ms=7200000, duration=7200,
                                 read_ms=lambda start, end: np.zeros((end - start) * 16, dtype=np.int16))
        result = [{"sentence_info": [{"text": "前句。后句。", "raw_text": "前 句 后 句",
                                      "timestamp": [[300, 400], [400, 500], [1100, 1200], [1200, 1300]],
                                      "start": 300, "end": 1600}]}]
        transcriber = Transcriber(AsrOptions())
        transcriber._model = SimpleNamespace(generate=Mock(return_value=result))
        progress = []
        with patch("lubo.asr.WavReader", return_value=nullcontext(reader)), \
                patch.object(transcriber, "_run_vad", return_value=segments), \
                patch.dict("sys.modules", {"torch": SimpleNamespace()}):
            cues = transcriber.transcribe("fixture.wav", 22, on_progress=progress.append)
        self.assertEqual(len(cues), 120)
        self.assertEqual(transcriber._model.generate.call_count, 60)
        for index in range(60):
            self.assertAlmostEqual(cues[2 * index].start, index * 120 + 1, places=3)
            self.assertAlmostEqual(cues[2 * index + 1].start, index * 120 + 1.8, places=3)
            self.assertAlmostEqual(cues[2 * index + 1].end, index * 120 + 2, places=3)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 1)


if __name__ == "__main__":
    unittest.main()
