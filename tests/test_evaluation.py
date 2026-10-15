import unittest

from lubo.evaluation import score_subtitles, score_text
from lubo.subtitles import Cue


class EvaluationTests(unittest.TestCase):
    def test_cer_counts_substitution_insertion_and_deletion(self):
        self.assertEqual(score_text("甲乙丙", "甲丁丙")["substitutions"], 1)
        self.assertEqual(score_text("甲乙丙", "甲乙")["deletions"], 1)
        self.assertEqual(score_text("甲乙", "甲乙丙")["insertions"], 1)
        self.assertAlmostEqual(score_text("甲乙丙", "甲丁丙")["cer"], 1 / 3)

    def test_silence_hallucination_is_not_reported_as_zero_cer(self):
        score = score_text("", "凭空生成字幕")
        self.assertIsNone(score["cer"])
        self.assertEqual(score["silence_hallucination_chars"], 6)

    def test_perfect_subtitles(self):
        cues = [Cue(0, 1, "第一句话"), Cue(2, 3, "第二句话")]
        result = score_subtitles(cues, cues)
        self.assertEqual(result["text"]["cer"], 0)
        self.assertEqual(result["boundaries"]["f1"], 1)
        self.assertEqual(result["timing"]["coverage"], 1)
        self.assertEqual(result["timing"]["onset"]["max_absolute_ms"], 0)
        self.assertIsNone(result["timing"]["onset_drift_ms_per_hour"])

    def test_merged_sentences_are_penalized_even_with_correct_words(self):
        reference = [Cue(0, 1, "前一句结束了"), Cue(2, 3, "后一句开始了")]
        hypothesis = [Cue(0, 3, "前一句结束了后一句开始了")]
        result = score_subtitles(reference, hypothesis)
        self.assertEqual(result["text"]["cer"], 0)
        self.assertEqual(result["boundaries"]["f1"], 0)
        self.assertEqual(result["timing"]["onset"]["max_absolute_ms"], 2000)

    def test_constant_offset_is_distinct_from_progressive_drift(self):
        reference = [Cue(i * 60, i * 60 + 1, f"第{i}句内容") for i in range(5)]
        shifted = [Cue(c.start + 2, c.end + 2, c.text) for c in reference]
        drifting = [Cue(c.start * 1.01, c.end * 1.01, c.text) for c in reference]
        shifted_score = score_subtitles(reference, shifted)["timing"]
        drift_score = score_subtitles(reference, drifting)["timing"]
        self.assertEqual(shifted_score["onset"]["median_absolute_ms"], 2000)
        self.assertEqual(shifted_score["onset_drift_ms_per_hour"], 0)
        self.assertAlmostEqual(drift_score["onset_drift_ms_per_hour"], 36000, places=5)

    def test_missing_sentence_reduces_timing_coverage(self):
        reference = [Cue(0, 1, "甲乙丙丁"), Cue(2, 3, "戊己庚辛")]
        result = score_subtitles(reference, reference[:1])
        self.assertEqual(result["text"]["cer"], .5)
        self.assertEqual(result["timing"]["coverage"], .5)
        self.assertFalse(result["timing"]["intervals"][1]["scored"])

    def test_normalization_does_not_erase_numbers_or_change_words(self):
        self.assertEqual(score_text("Python ３.１０，正常。", "python 3.10正常")["cer"], 0)
        self.assertGreater(score_text("三点一零", "3.10")["cer"], 0)


if __name__ == "__main__":
    unittest.main()
