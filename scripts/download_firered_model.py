"""Download and cryptographically verify the pinned FireRedASR2-AED model."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from firered_runtime import (
    CODE_REVISION,
    CODE_REPO,
    MARKER_NAME,
    MODEL_FILES,
    MODEL_REPO,
    MODEL_REVISION,
    check_code_revision,
    file_record,
    git_blob_sha1,
    resolve_code_dir,
    resolve_model_dir,
    sha256_file,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def verify_files(model_dir: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for name, expected in MODEL_FILES.items():
        path = model_dir / name
        if not path.is_file():
            raise RuntimeError(f"Downloaded FireRed file is missing: {path}")
        if path.stat().st_size != expected["size"]:
            raise RuntimeError(
                f"Downloaded FireRed file has the wrong size: {path} "
                f"({path.stat().st_size}; expected {expected['size']})"
            )
        actual = sha256_file(path)
        if "sha256" in expected and actual != expected["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {path}: {actual}")
        if "git_blob_sha1" in expected:
            actual_git_oid = git_blob_sha1(path)
            if actual_git_oid != expected["git_blob_sha1"]:
                raise RuntimeError(f"Pinned Git object mismatch for {path}: {actual_git_oid}")
        record = file_record(path)
        if "git_blob_sha1" in expected:
            record["git_blob_sha1"] = expected["git_blob_sha1"]
        records[name] = record
        print(f"verified {name}: {record['size']} bytes sha256={record['sha256']}", flush=True)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=resolve_model_dir())
    parser.add_argument("--code-dir", type=Path, default=resolve_code_dir())
    args = parser.parse_args()
    model_dir = args.model_dir.expanduser().resolve()
    code_dir = args.code_dir.expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    marker_path = model_dir / MARKER_NAME
    write_json_atomic(
        marker_path,
        {
            "schema_version": 1,
            "repo_id": MODEL_REPO,
            "revision": MODEL_REVISION,
            "code_repo": CODE_REPO,
            "code_revision": CODE_REVISION,
            "verified": False,
            "verified_at_utc": None,
            "files": {},
        },
    )
    check_code_revision(code_dir)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is missing from the FireRed environment; "
            "run scripts/setup_firered.ps1 first."
        ) from exc

    print(f"Downloading {MODEL_REPO}@{MODEL_REVISION} into {model_dir}", flush=True)
    snapshot_download(
        repo_id=MODEL_REPO,
        revision=MODEL_REVISION,
        local_dir=str(model_dir),
        allow_patterns=list(MODEL_FILES),
    )
    records = verify_files(model_dir)
    marker = {
        "schema_version": 1,
        "repo_id": MODEL_REPO,
        "revision": MODEL_REVISION,
        "code_repo": CODE_REPO,
        "code_revision": CODE_REVISION,
        "verified": True,
        "verified_at_utc": utc_now(),
        "files": records,
    }
    write_json_atomic(marker_path, marker)
    print(f"FireRed model verified. Marker: {marker_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FireRed model setup failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
