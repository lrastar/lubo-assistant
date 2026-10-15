import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import firered_worker


class FireRedReportIoTests(unittest.TestCase):
    def test_transient_windows_reader_lock_preserves_atomic_publication(self):
        with TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            report.write_text('{"completed_count": 74}', encoding="utf-8")
            replace = Path.replace
            attempts = []

            def contend(source, target):
                attempts.append(source)
                if len(attempts) == 1:
                    self.assertEqual(json.loads(report.read_text())["completed_count"], 74)
                    error = PermissionError("reader still holds report")
                    error.winerror = 5
                    raise error
                return replace(source, target)

            with patch.object(Path, "replace", contend):
                firered_worker.write_json_atomic(report, {"completed_count": 75})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(report.read_text())["completed_count"], 75)
            self.assertFalse(report.with_name("report.json.part").exists())

    def test_unrelated_io_failure_is_not_retried(self):
        with TemporaryDirectory() as folder:
            with patch.object(Path, "replace", side_effect=OSError("disk failure")) as replace:
                with self.assertRaisesRegex(OSError, "disk failure"):
                    firered_worker.write_json_atomic(Path(folder) / "report.json", {})
            self.assertEqual(replace.call_count, 1)

    def test_persistent_lock_stops_at_deadline(self):
        error = PermissionError("persistent lock")
        error.winerror = 32
        with TemporaryDirectory() as folder:
            with patch.object(Path, "replace", side_effect=error) as replace, \
                    patch.object(firered_worker.time, "monotonic", side_effect=[0, 3]):
                with self.assertRaisesRegex(PermissionError, "persistent lock"):
                    firered_worker.write_json_atomic(Path(folder) / "report.json", {})
            self.assertEqual(replace.call_count, 1)


if __name__ == "__main__":
    unittest.main()
