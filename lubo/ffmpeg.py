from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

STDERR_TAIL_LINES = 30
CREATE_NO_WINDOW = 0x08000000
_POLL_INTERVAL = 0.1
_KILL_TIMEOUT = 2.0


class FfmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str | None
    sample_rate: int | None
    channels: int | None
    has_audio: bool


def _creationflags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


_tool_cache: dict[str, str] = {}


def find_tool(name: str) -> str:
    if name in _tool_cache:
        return _tool_cache[name]
    exe = f"{name}.exe" if sys.platform == "win32" else name
    local = _app_dir() / "bin" / exe
    found = str(local) if local.is_file() else shutil.which(name)
    if not found:
        raise FfmpegError(f"找不到 {name}：请把 {exe} 放到程序目录的 bin 文件夹，或加入 PATH")
    _tool_cache[name] = found
    return found


def _run_capture(cmd: list[str], timeout: float | None = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=_creationflags(),
    )


def _parse_rate(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except ValueError:
        return 0.0


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    cmd = [find_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        completed = _run_capture(cmd)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffprobe 超时：{path}") from exc
    if completed.returncode != 0:
        raise FfmpegError(f"ffprobe 失败：{path}\n{completed.stderr.strip()}")
    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise FfmpegError(f"ffprobe 输出无法解析：{path}") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FfmpegError(f"没有视频流：{path}")

    duration = _to_float((data.get("format") or {}).get("duration"))
    if duration <= 0:
        duration = max((_to_float(s.get("duration")) for s in streams), default=0.0)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return MediaInfo(
        path=path,
        duration=duration,
        width=_to_int(video.get("width")) or 0,
        height=_to_int(video.get("height")) or 0,
        fps=fps,
        vcodec=str(video.get("codec_name") or ""),
        acodec=str(audio.get("codec_name")) if audio else None,
        sample_rate=_to_int(audio.get("sample_rate")) if audio else None,
        channels=_to_int(audio.get("channels")) if audio else None,
        has_audio=audio is not None,
    )


_encoder_cache: set[str] | None = None
_encoder_lock = threading.Lock()


def _list_encoders() -> set[str]:
    completed = _run_capture([find_tool("ffmpeg"), "-hide_banner", "-encoders"], timeout=60)
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        # 形如 " V....D h264_nvenc   NVIDIA NVENC H.264 encoder"
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" and parts[1] != "=":
            names.add(parts[1])
    return names


def has_encoder(name: str) -> bool:
    global _encoder_cache
    with _encoder_lock:
        if _encoder_cache is None:
            try:
                _encoder_cache = _list_encoders()
            except (FfmpegError, OSError, subprocess.SubprocessError):
                return False
        return name in _encoder_cache


def parse_progress_seconds(key: str, value: str) -> float | None:
    """解析 -progress 输出里的时间字段（out_time_ms 实际单位也是微秒）。"""
    value = value.strip()
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(value) / 1_000_000
        except ValueError:
            return None
    if key == "out_time":
        parts = value.split(":")
        if len(parts) != 3:
            return None
        try:
            sign = -1 if parts[0].startswith("-") else 1
            return sign * (abs(int(parts[0])) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        except ValueError:
            return None
    return None


def _pump(stream: IO[str], out: queue.Queue, kind: str) -> None:
    try:
        for line in stream:
            out.put((kind, line.rstrip("\r\n")))
    except (OSError, ValueError):
        pass


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_KILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        pass


def run_ffmpeg(args: list[str], duration: float,
               on_progress: Callable[[float], None] | None = None,
               cancel: threading.Event | None = None,
               log: Callable[[str], None] | None = None,
               cwd: Path | None = None) -> None:
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats", "-loglevel", "error", *args]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd is not None else None,
        creationflags=_creationflags(),
    )
    lines: queue.Queue = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(process.stdout, lines, "out"), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, lines, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    last_fraction = -1.0

    def report(fraction: float) -> None:
        nonlocal last_fraction
        fraction = min(1.0, max(0.0, fraction))
        if on_progress is not None and fraction > last_fraction:
            last_fraction = fraction
            on_progress(fraction)

    # 回调都在调用线程里执行（读线程只负责把行放进队列）
    def drain() -> None:
        while True:
            try:
                kind, line = lines.get_nowait()
            except queue.Empty:
                return
            if kind == "err":
                if line.strip():
                    tail.append(line)
                    if log is not None:
                        log(line)
                continue
            key, _, value = line.partition("=")
            if key == "progress" and value.strip() == "end":
                report(1.0)
            elif duration > 0:
                seconds = parse_progress_seconds(key, value)
                if seconds is not None:
                    report(seconds / duration)

    try:
        while True:
            if cancel is not None and cancel.is_set():
                _stop_process(process)
                raise Cancelled("已取消")
            try:
                process.wait(timeout=_POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                drain()
        for reader in readers:
            reader.join(timeout=5)
        drain()
    finally:
        _stop_process(process)
        for reader, stream in zip(readers, (process.stdout, process.stderr)):
            reader.join(timeout=2)
            # 读线程仍阻塞时关闭会争用同一把锁，交给守护线程自行结束
            if stream is not None and not reader.is_alive():
                try:
                    stream.close()
                except OSError:
                    pass

    if process.returncode != 0:
        detail = "\n".join(tail) or "（无错误输出）"
        raise FfmpegError(f"ffmpeg 执行失败（退出码 {process.returncode}）：\n{detail}")
    report(1.0)
