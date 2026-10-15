import copy
import unittest

from scripts.evaluate_asr import evaluate


class EvaluationRunTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"name": "independent-fixture", "samples": [
            {"id": "a", "audio_sha256": "audio-a", "reference_text": "甲乙",
             "reference_origin": "dataset", "reference_cues": None},
            {"id": "b", "audio_sha256": "audio-b", "reference_text": "丙丁",
             "reference_origin": "human_reviewed", "reference_cues": None},
        ]}
        self.candidate = {"complete": True, "ok": True, "engine": "fixture", "samples": [
            {"id": "a", "audio_sha256": "audio-a", "text": "甲乙"},
            {"id": "b", "audio_sha256": "audio-b", "text": "丙戊"},
        ]}

    def test_full_corpus_score_is_weighted_by_reference_characters(self):
        report = evaluate(self.manifest, self.candidate)
        self.assertEqual(report["corpus_cer"], .25)
        self.assertTrue(report["complete_coverage"])
        self.assertFalse(report["timing_reference_available"])
        self.assertFalse(report["jianying_comparison_available"])

    def test_failed_samples_do_not_produce_a_misleading_full_corpus_score(self):
        self.candidate["samples"].pop()
        report = evaluate(self.manifest, self.candidate)
        self.assertIsNone(report["corpus_cer"])
        self.assertEqual(report["failed_samples"], ["b"])
        self.assertFalse(report["complete_coverage"])

    def test_in_progress_runs_are_not_scored(self):
        self.candidate["complete"] = False
        with self.assertRaises(ValueError):
            evaluate(self.manifest, self.candidate)

    def test_different_audio_is_rejected(self):
        self.candidate["samples"][0]["audio_sha256"] = "other-recording"
        with self.assertRaises(ValueError):
            evaluate(self.manifest, self.candidate)

    def test_unreviewed_model_output_is_not_ground_truth(self):
        self.manifest["samples"][0]["reference_origin"] = "model_output"
        with self.assertRaises(ValueError):
            evaluate(self.manifest, self.candidate)

    def test_duplicate_sample_ids_are_rejected(self):
        self.candidate["samples"].append(copy.deepcopy(self.candidate["samples"][0]))
        with self.assertRaises(ValueError):
            evaluate(self.manifest, self.candidate)

    def test_rendered_subtitle_text_is_scored_independently(self):
        self.candidate["samples"][0]["cues"] = [{"start": 0, "end": 1, "text": "甲"}]
        self.candidate["samples"][1]["cues"] = [{"start": 0, "end": 1, "text": "丙戊"}]
        result = evaluate(self.manifest, self.candidate)
        self.assertEqual(result["corpus_cer"], .25)
        self.assertEqual(result["rendered_corpus_cer"], .5)


if __name__ == "__main__":
    unittest.main()
