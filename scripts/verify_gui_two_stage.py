"""Run the GUI's two subtitle stages on a generated, silent one-second clip."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import traceback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the result as UTF-8 JSON")
    parser.add_argument("--screenshot", type=Path,
                        help="Save an offscreen screenshot of the source GUI before stage one")
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory(prefix="lubo-gui-two-stage-") as temporary:
        root = Path(temporary)
        app_data = root / "AppData"
        local_data = root / "LocalAppData"
        app_data.mkdir()
        local_data.mkdir()
        os.environ["APPDATA"] = str(app_data)
        os.environ["LOCALAPPDATA"] = str(local_data)
        os.environ["LUBO_WORK_DIR"] = str(root / "work")

        project_root = Path(__file__).resolve().parent.parent
        os.environ["PATH"] = str(project_root.parent.parent / "bin") + os.pathsep + os.environ.get("PATH", "")
        import sys

        sys.path.insert(0, str(project_root))
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication
        from lubo.ffmpeg import find_tool, probe
        from lubo.gui.main_window import MainWindow, _set_widget_value
        from lubo.segments import Segment

        video = root / "generated-silent.mp4"
        ffmpeg = find_tool("ffmpeg")
        subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=320x240:r=10", "-t", "1", "-an",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
             "-pix_fmt", "yuv420p", str(video)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        font_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        loaded_font_families = []
        for font_file in (font_root / "msyh.ttc", font_root / "NotoSansSC-VF.ttf"):
            if font_file.is_file():
                font_id = QFontDatabase.addApplicationFont(str(font_file))
                if font_id >= 0:
                    loaded_font_families.extend(QFontDatabase.applicationFontFamilies(font_id))
        window = MainWindow(settings_file=root / "settings.json")
        dialogs: list[str] = []
        window._message = lambda _icon, title, message: dialogs.append(f"{title}: {message}")
        window.output_dir_edit.setText(str(root / "output"))
        window.prefix_edit.setText("two-stage-smoke")
        widgets = {(binding.section, binding.field): binding.widget for binding in window._bindings}
        _set_widget_value(widgets[("encode", "encoder")], "libx264")
        _set_widget_value(widgets[("encode", "preset")], "ultrafast")
        window._add_segment_item(
            Segment(video, None, None, None, video.stat().st_size, info=probe(video)))

        def wait_for_stage(action, label: str) -> list[str]:
            loop = QEventLoop()
            outcome: dict[str, object] = {"finished": None, "failed": None, "cancelled": False}
            outcome["finished_slot"] = lambda paths: outcome.__setitem__("finished", paths)
            outcome["failed_slot"] = lambda message: outcome.__setitem__("failed", message)
            outcome["cancelled_slot"] = lambda: outcome.__setitem__("cancelled", True)
            outcome["running_slot"] = lambda running: loop.quit() if not running else None
            window._job_controller.finished.connect(outcome["finished_slot"])
            window._job_controller.failed.connect(outcome["failed_slot"])
            window._job_controller.cancelled.connect(outcome["cancelled_slot"])
            window._job_controller.runningChanged.connect(outcome["running_slot"])
            QTimer.singleShot(120_000, loop.quit)
            action()
            if not window.is_running():
                raise RuntimeError(f"GUI did not start the {label} stage: {dialogs}")
            loop.exec()
            for signal, slot in (
                (window._job_controller.finished, outcome["finished_slot"]),
                (window._job_controller.failed, outcome["failed_slot"]),
                (window._job_controller.cancelled, outcome["cancelled_slot"]),
                (window._job_controller.runningChanged, outcome["running_slot"]),
            ):
                signal.disconnect(slot)
            if window.is_running():
                window.cancel()
                shutdown_loop = QEventLoop()
                shutdown_slot = lambda running: shutdown_loop.quit() if not running else None
                window._job_controller.runningChanged.connect(shutdown_slot)
                QTimer.singleShot(30_000, shutdown_loop.quit)
                shutdown_loop.exec()
                window._job_controller.runningChanged.disconnect(shutdown_slot)
                if window.is_running():
                    raise RuntimeError(f"Timed-out {label} stage did not stop after cancellation")
                raise TimeoutError(f"GUI {label} stage did not finish within 120 seconds")
            if outcome["failed"]:
                raise RuntimeError(f"GUI {label} stage failed: {outcome['failed']}\n{dialogs}")
            if outcome["cancelled"]:
                raise RuntimeError(f"GUI {label} stage was unexpectedly cancelled")
            if outcome["finished"] is None:
                raise RuntimeError(f"GUI {label} stage ended without a terminal result")
            app.processEvents()
            return [str(path) for path in outcome["finished"]]

        window.show()
        app.processEvents()
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot)):
                raise RuntimeError(f"Unable to save the GUI screenshot: {args.screenshot}")
        first_outputs = wait_for_stage(window.generate_subtitles, "generate")
        srt = root / "output" / "two-stage-smoke.srt"
        if not srt.is_file():
            raise RuntimeError(f"First stage did not create the expected SRT: {srt}")
        edited_text = "1\n00:00:00,000 --> 00:00:00,900\n手工校对字幕保留\n\n"
        srt.write_text(edited_text, encoding="utf-8")
        second_outputs = wait_for_stage(window.burn_subtitles, "burn")
        final_video = root / "output" / "two-stage-smoke.完整版.mp4"
        if not final_video.is_file() or probe(final_video).duration <= 0:
            raise RuntimeError(f"Second stage did not create a playable final video: {final_video}")
        if srt.read_text(encoding="utf-8") != edited_text:
            raise RuntimeError("The burn stage changed the manually edited SRT")
        report = {
            "ok": True,
            "generated_video_seconds": round(probe(video).duration, 3),
            "generated_srt": True,
            "manual_srt_preserved": True,
            "final_video_seconds": round(probe(final_video).duration, 3),
            "stage_one_outputs": first_outputs,
            "stage_two_outputs": second_outputs,
            "settings_path_was_temporary": str(root / "settings.json"),
            "work_directory_was_temporary": str(root / "work"),
            "screenshot_font_families_loaded": loaded_font_families,
            "screenshot": str(args.screenshot.resolve()) if args.screenshot else None,
        }
        if args.report:
            report_path = args.report.resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report["report_path"] = str(report_path)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        window.close()
        window.deleteLater()
        app.processEvents()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
