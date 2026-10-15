"""录播助手入口：python main.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _redirect_missing_streams() -> None:
    # pythonw 下 stdout/stderr 为 None，tqdm/funasr 等写 stderr 会崩溃，改写到日志文件
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = Path(os.environ.get("APPDATA") or Path.home()) / "LuboStudio"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "console.log", "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> int:
    if "--quality-worker" in sys.argv:
        sys.argv.remove("--quality-worker")
        from lubo.quality_worker import main as worker_main
        worker_main()
        return 0
    _redirect_missing_streams()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lubo.gui.main_window import run_app

    return run_app()


if __name__ == "__main__":
    # 冻结版中 Torch/FunASR 可能启动 multiprocessing 子进程；先让其进入
    # Python 的工作进程入口，避免子进程再次打开桌面主窗口。
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
