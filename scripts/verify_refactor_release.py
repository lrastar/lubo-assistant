"""Verify a packaged refactor build without loading ASR weights or user settings."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import struct
import subprocess
import sys

from PyInstaller.archive.readers import CArchiveReader


REQUIRED_MODULES = {
    "jieba",
    "lubo.artifact_store",
    "lubo.asr_adapters",
    "lubo.asr_backend",
    "lubo.asr_profiles",
    "lubo.asr_timing",
    "lubo.config",
    "lubo.gui.job_controller",
    "lubo.gui.main_window",
    "lubo.pipeline",
    "lubo.quality_checkpoint",
    "lubo.quality_recording",
    "lubo.quality_worker",
    "lubo.wav_audio",
    "transformers.models.qwen3_asr.configuration_qwen3_asr",
    "transformers.models.qwen3_asr.feature_extraction_qwen3_asr",
    "transformers.models.qwen3_asr.modeling_qwen3_asr",
    "transformers.models.qwen3_asr.processing_qwen3_asr",
}
FIRERED_SIDECARS = ("firered_worker.py", "firered_runtime.py")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pyz_members(exe: Path) -> tuple[CArchiveReader, set[str]]:
    archive = CArchiveReader(str(exe))
    pyz = archive.extract("PYZ.pyz")
    if pyz[4:8] != importlib.util.MAGIC_NUMBER:
        raise RuntimeError(
            f"Verifier Python {sys.version_info.major}.{sys.version_info.minor} bytecode "
            f"does not match the bundled executable: {exe}"
        )
    table_offset = struct.unpack("!i", pyz[8:12])[0]
    table = marshal.loads(pyz[table_offset:])
    return archive, set(dict(table))


def verify_dependency_archive(base_exe: Path, exe: Path) -> None:
    base = CArchiveReader(str(base_exe))
    updated = CArchiveReader(str(exe))
    if base.options != updated.options:
        raise RuntimeError("Updated EXE has different PyInstaller options from the base")
    if set(base.toc) != set(updated.toc):
        raise RuntimeError("Updated EXE has a different CArchive member list")
    for name in base.toc:
        if name in ("main", "PYZ.pyz"):
            continue
        if base.extract(name) != updated.extract(name):
            raise RuntimeError(f"Bundled dependency changed: {name}")


def verify(args: argparse.Namespace) -> dict[str, object]:
    exe = args.exe.resolve()
    app_dir = args.app_dir.resolve()
    if not exe.is_file() or exe.parent != app_dir:
        raise FileNotFoundError(f"Expected an EXE inside the app directory: {exe}")
    for protected in args.protected_exe:
        if exe == protected.resolve():
            raise RuntimeError(f"The output EXE is one of the protected release entries: {exe}")

    _, modules = pyz_members(exe)
    missing = sorted(REQUIRED_MODULES - modules)
    if missing:
        raise RuntimeError("Packaged PYZ is missing required modules: " + ", ".join(missing))

    dictionary = app_dir / "jieba" / "dict.txt"
    if not dictionary.is_file() or dictionary.stat().st_size == 0:
        raise RuntimeError(f"Missing offline Jieba dictionary: {dictionary}")
    support_dir = app_dir / "quality_support"
    for name in FIRERED_SIDECARS:
        path = support_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing FireRed worker support file: {path}")

    if args.source_root:
        source_root = args.source_root.resolve()
        for name in FIRERED_SIDECARS:
            source = source_root / "scripts" / name
            target = support_dir / name
            if sha256(source) != sha256(target):
                raise RuntimeError(f"Packaged FireRed support file differs from source: {name}")
    if args.base_exe:
        verify_dependency_archive(args.base_exe.resolve(), exe)

    worker_help_checked = False
    if args.smoke_worker_entry:
        result = subprocess.run(
            [str(exe), "--quality-worker", "--help"],
            cwd=app_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Packaged quality-worker entrypoint exited {result.returncode}; "
                f"stdout={result.stdout[-1000:]!r}; stderr={result.stderr[-1000:]!r}"
            )
        worker_help_checked = True

    protected_hashes = {str(path.resolve()): sha256(path.resolve()) for path in args.protected_exe}
    return {
        "ok": True,
        "exe": str(exe),
        "sha256": sha256(exe),
        "pyz_module_count": len(modules),
        "required_modules": sorted(REQUIRED_MODULES),
        "jieba_dictionary_bytes": dictionary.stat().st_size,
        "firered_sidecars": {name: sha256(support_dir / name) for name in FIRERED_SIDECARS},
        "dependency_archive_checked": bool(args.base_exe),
        "worker_entrypoint_smoke_checked": worker_help_checked,
        "protected_exes": protected_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--base-exe", type=Path,
                        help="Also verify every embedded dependency except main/PYZ stays byte-identical")
    parser.add_argument("--protected-exe", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path,
                        help="Write the successful verification result as UTF-8 JSON")
    parser.add_argument("--smoke-worker-entry", action="store_true",
                        help="Start the packaged Qwen worker CLI with --help; does not load weights")
    args = parser.parse_args()
    try:
        result = verify(args)
    except BaseException as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    if args.report:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        result["report_path"] = str(report_path)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
