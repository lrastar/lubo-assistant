"""Pinned FireRedASR2-AED artifact identities and runtime path helpers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


MODEL_REPO = "FireRedTeam/FireRedASR2-AED"
MODEL_REVISION = "2304afed56eacfee6256dee5937ed22ffa0b64ec"
CODE_REPO = "https://github.com/FireRedTeam/FireRedASR2S.git"
CODE_REVISION = "4e7d9aaf4482a47cec1724807026b9b151926eb5"
MODEL_NAME = "FireRedASR2-AED"
MODEL_FILES: dict[str, dict[str, Any]] = {
    "cmvn.ark": {
        "size": 1311,
        "git_blob_sha1": "e26b4a310132492d05bfdd506ab3f6bbf89d9059",
    },
    "config.yaml": {
        "size": 0,
        "git_blob_sha1": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    },
    "dict.txt": {
        "size": 79172,
        "git_blob_sha1": "50ac9cbd43d638b58f875e90b118ad8c9be51718",
    },
    "model.pth.tar": {
        "size": 4731558506,
        "sha256": "4677cbd30988d63ed3e777f6a42a1e5260a3865317f6e15e488bef40954f7054",
    },
    "train_bpe1000.model": {
        "size": 251707,
        "sha256": "473bbc157cb4eade2059b30a3c877a1c29bd50cadbfbed869ae36eeade7fee07",
    },
}
MARKER_NAME = "lubo-model.json"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    # Match lubo.asr_backend.runtime_home_directory(): installed app layouts
    # keep runtimes next to the distribution; a standalone source clone keeps
    # them inside the project unless an adjacent app runtime already exists.
    source_root = project_root()
    installed_root = source_root.parent.parent
    return installed_root if (installed_root / "asr_runtimes").is_dir() else source_root


def runtime_root() -> Path:
    value = os.environ.get("LUBO_ASR_RUNTIMES")
    return Path(value).expanduser().resolve() if value else app_root() / "asr_runtimes"


def resolve_model_dir(value: str | None = None) -> Path:
    value = value or os.environ.get("LUBO_FIRERED_MODEL_DIR")
    return Path(value).expanduser().resolve() if value else runtime_root() / "fireredasr2-aed"


def resolve_code_dir(value: str | None = None) -> Path:
    value = value or os.environ.get("LUBO_FIRERED_CODE_DIR")
    return Path(value).expanduser().resolve() if value else runtime_root() / "FireRedASR2S"


def check_code_revision(code_dir: Path) -> None:
    if not (code_dir / "fireredasr2s" / "fireredasr2" / "asr.py").is_file():
        raise RuntimeError(f"FireRed source checkout is incomplete: {code_dir}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=code_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            f"Cannot verify FireRed code revision in {code_dir}; ensure git is installed and the checkout is valid."
        ) from exc
    actual = result.stdout.strip()
    if actual != CODE_REVISION:
        raise RuntimeError(f"FireRed code revision {actual!r}; expected {CODE_REVISION}")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=code_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Cannot inspect FireRed source changes in {code_dir}") from exc
    if status.stdout.strip():
        raise RuntimeError(f"FireRed source checkout has local changes: {code_dir}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    return {
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def read_marker(model_dir: Path, *, check_files: bool = True) -> dict[str, Any]:
    marker_path = model_dir / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"FireRed model is not verified; missing or unreadable marker: {marker_path}. "
            "Run scripts/setup_firered.ps1 to download and verify it."
        ) from exc
    if not isinstance(marker, dict):
        raise RuntimeError(f"FireRed model marker must contain a JSON object: {marker_path}")

    expected_marker = {
        "schema_version": 1,
        "repo_id": MODEL_REPO,
        "revision": MODEL_REVISION,
        "code_repo": CODE_REPO,
        "code_revision": CODE_REVISION,
        "verified": True,
    }
    for key, expected in expected_marker.items():
        if marker.get(key) != expected:
            raise RuntimeError(
                f"FireRed model marker {key} is {marker.get(key)!r}; expected {expected!r}. "
                "Run scripts/setup_firered.ps1 to install the pinned runtime."
            )

    marker_files = marker.get("files")
    if not isinstance(marker_files, dict):
        raise RuntimeError(f"FireRed model marker has no file manifest: {marker_path}")
    for name, expected in MODEL_FILES.items():
        recorded = marker_files.get(name)
        path = model_dir / name
        if not isinstance(recorded, dict) or not path.is_file():
            raise RuntimeError(f"FireRed model file is missing or unverified: {path}")
        if recorded.get("size") != expected["size"] or path.stat().st_size != expected["size"]:
            raise RuntimeError(f"FireRed model file size does not match the pinned revision: {path}")
        if "sha256" in expected and recorded.get("sha256") != expected["sha256"]:
            raise RuntimeError(f"FireRed model marker SHA-256 mismatch: {path}")
        if "git_blob_sha1" in expected and recorded.get("git_blob_sha1") != expected["git_blob_sha1"]:
            raise RuntimeError(f"FireRed model marker Git object mismatch: {path}")
        if check_files:
            if "sha256" in expected:
                actual = sha256_file(path)
            else:
                actual = git_blob_sha1(path)
            if actual != expected.get("sha256", expected.get("git_blob_sha1")):
                raise RuntimeError(f"FireRed model file checksum mismatch: {path}")
        recorded_mtime = recorded.get("mtime_ns")
        if recorded_mtime is not None and path.stat().st_mtime_ns != recorded_mtime:
            raise RuntimeError(
                f"FireRed model file changed after verification: {path}. "
                "Re-run scripts/setup_firered.ps1 to verify the installed model."
            )
    return marker
