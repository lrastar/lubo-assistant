"""Start the packaged GUI briefly with isolated temporary user directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--startup-seconds", type=float, default=5.0)
    args = parser.parse_args()
    exe = args.exe.resolve()
    if not exe.is_file():
        parser.error(f"EXE does not exist: {exe}")
    if args.startup_seconds <= 0 or args.startup_seconds > 60:
        parser.error("--startup-seconds must be in (0, 60]")

    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lubo-exe-gui-startup-") as temporary:
        root = Path(temporary)
        appdata = root / "Roaming"
        local_appdata = root / "Local"
        work = root / "work"
        for directory in (appdata, local_appdata, work):
            directory.mkdir(parents=True)
        settings_file = appdata / "LuboStudio" / "settings.json"
        log_path = root / "startup.log"
        environment = dict(os.environ)
        environment.update({
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(local_appdata),
            "LUBO_WORK_DIR": str(work),
            "QT_QPA_PLATFORM": "offscreen",
            "HF_HOME": str(root / "hf"),
            "TORCH_HOME": str(root / "torch"),
        })
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with log_path.open("wb") as log_stream:
            process = subprocess.Popen(
                [str(exe)], cwd=exe.parent, env=environment,
                stdin=subprocess.DEVNULL, stdout=log_stream, stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            started = time.monotonic()
            deadline = started + args.startup_seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            startup_elapsed = time.monotonic() - started
            exited_early = process.poll() is not None
            return_code = process.returncode
            if not exited_early:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if exited_early:
            raise RuntimeError(
                f"GUI EXE exited during startup with {return_code}; log={log_text[-4000:]}"
            )
        report = {
            "ok": True,
            "exe": str(exe),
            "exe_sha256": sha256(exe),
            "qt_platform": "offscreen",
            "startup_seconds": round(startup_elapsed, 2),
            "process_remained_alive": True,
            "cleanup_terminated_process": True,
            "settings_isolated_under_temporary_appdata": True,
            "temporary_settings_path": str(settings_file),
            "settings_file_created": settings_file.is_file(),
            "startup_log_tail": log_text[-2000:],
            "report_path": str(report_path),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
