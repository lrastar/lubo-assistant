import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lubo.config import SETTINGS_SCHEMA_VERSION, load_settings, save_settings


class SettingsSchemaTests(unittest.TestCase):
    def test_legacy_explicit_firered_selection_survives_migration(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"asr": {
                "engine": "quality", "quality_model": "firered_asr2_aed",
                "context": "保存的 Qwen 词汇提示", "language": "Chinese",
            }}), encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual(settings.asr.quality_model, "firered_asr2_aed")
            self.assertEqual(settings.asr.context, "保存的 Qwen 词汇提示")
            self.assertEqual(settings.asr.language, "Chinese")
            self.assertFalse(any("继续使用 Qwen" in warning for warning in settings.load_warnings))

    def test_legacy_preferences_migrate_without_changing_qwen_defaults(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({
                "asr": {
                    "engine": "quality",
                    "context": "主播和游戏角色名",
                    "language": "Chinese",
                    "model": "paraformer-zh",
                },
                "last_segments": ["clip.mp4"],
            }), encoding="utf-8")

            settings = load_settings(path)

            self.assertEqual(settings.schema_version, SETTINGS_SCHEMA_VERSION)
            self.assertEqual(settings.asr.quality_model, "qwen3")
            self.assertEqual(settings.asr.context, "主播和游戏角色名")
            self.assertEqual(settings.asr.language, "Chinese")
            self.assertEqual(settings.asr.model, "paraformer-zh")
            self.assertEqual(settings.last_segments, ["clip.mp4"])
            self.assertTrue(any("迁移" in warning for warning in settings.load_warnings))

            save_settings(settings, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertNotIn("load_warnings", stored)

    def test_invalid_enums_and_non_finite_or_out_of_range_values_recover_with_diagnostics(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({
                "schema_version": SETTINGS_SCHEMA_VERSION,
                "theme": "sepia",
                "asr": {
                    "engine": "unknown-engine",
                    "quality_model": "made-up-model",
                    "chunk_seconds": float("nan"),
                },
                "danmaku": {"opacity": 500},
            }), encoding="utf-8")

            settings = load_settings(path)

            self.assertEqual(settings.theme, "dark")
            self.assertEqual(settings.asr.engine, "quality")
            self.assertEqual(settings.asr.quality_model, "qwen3")
            self.assertEqual(settings.asr.chunk_seconds, 120.0)
            self.assertEqual(settings.danmaku.opacity, 40)
            diagnostics = "\n".join(settings.load_warnings)
            self.assertIn("theme", diagnostics)
            self.assertIn("asr.engine", diagnostics)
            self.assertIn("asr.quality_model", diagnostics)
            self.assertIn("chunk_seconds", diagnostics)
            self.assertIn("opacity", diagnostics)


if __name__ == "__main__":
    unittest.main()
