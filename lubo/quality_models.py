"""Download pinned public Qwen checkpoints with resume and SHA-256 verification.

This uses only the standard library and never uploads audio or uses an API key.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


from lubo.asr_profiles import get_asr_profile


_QWEN3 = get_asr_profile("qwen3")
MODELS = {
    "asr": (_QWEN3.asr_repo, _QWEN3.asr_revision),
    "aligner": (_QWEN3.aligner_repo, _QWEN3.aligner_revision),
    "small": ("Qwen/Qwen3-ASR-0.6B-hf", "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c"),
    "nano": ("FunAudioLLM/Fun-ASR-Nano-2512-hf", "7ef6cafdc445b5beff0b730cbeb51952c793ca6e"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, path: Path, expected_size: int, digest: str | None) -> None:
    if path.is_file() and path.stat().st_size == expected_size:
        if digest is None or sha256(path) == digest:
            print(f"Verified cached {path.name}", flush=True)
            return
    partial = path.with_name(path.name + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(8):
        position = partial.stat().st_size if partial.exists() else 0
        if position == expected_size:
            break
        headers = {"User-Agent": "LuboStudio-model-download/1", "Accept-Encoding": "identity"}
        if position:
            headers["Range"] = f"bytes={position}-"
        try:
            # A new resolver request on each retry obtains a fresh public CDN URL.
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 206:
                    if not response.headers.get("Content-Range", "").startswith(f"bytes {position}-"):
                        raise OSError("Unexpected resume offset")
                    mode = "ab"
                else:
                    position = 0
                    mode = "wb"
                next_report = position + 64 * 1024 * 1024
                with partial.open(mode) as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                        position += len(chunk)
                        if position >= next_report:
                            print(f"{path.parent.name}/{path.name}: {position}/{expected_size} bytes "
                                  f"({position / expected_size:.1%})", flush=True)
                            next_report = position + 64 * 1024 * 1024
            if position == expected_size:
                break
            raise OSError(f"Incomplete download: {position}/{expected_size}")
        except (OSError, urllib.error.URLError) as error:
            print(f"Download retry {attempt + 1}: {type(error).__name__}", flush=True)
            if attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 15))
    if partial.stat().st_size != expected_size:
        raise OSError(f"File size mismatch for {path}")
    if digest and sha256(partial) != digest:
        raise OSError(f"SHA-256 mismatch for {partial}; retained for inspection")
    partial.replace(path)
    print(f"Verified {path}", flush=True)


def download_model(key: str, root: Path) -> None:
    repo, revision = MODELS[key]
    directory = (root / repo.split("/")[-1]).resolve()
    marker = directory / "lubo-model.json"
    required = {"config.json", "processor_config.json", "tokenizer.json", "tokenizer_config.json",
                "chat_template.jinja", "model.safetensors"}
    try:
        cached = json.loads(marker.read_text(encoding="utf-8"))
        if (cached.get("repo") == repo and cached.get("revision") == revision and cached.get("verified")
                and required.issubset(cached.get("required_files", []))
                and all((directory / name).is_file() and (directory / name).stat().st_size > 0 for name in required)):
            print(f"Using verified local checkpoint: {repo}", flush=True)
            return
    except (OSError, ValueError):
        pass
    metadata_url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
    with urllib.request.urlopen(metadata_url, timeout=60) as response:
        metadata = json.load(response)
    if metadata["sha"] != revision:
        raise ValueError("Model revision mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    for entry in metadata["siblings"]:
        name = entry["rfilename"]
        if Path(name).suffix not in (".json", ".safetensors", ".txt", ".model", ".md", ".jinja"):
            continue
        path = (directory / name).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Unsafe filename in model metadata")
        fetch(f"https://huggingface.co/{repo}/resolve/{revision}/{name}?download=true",
              path, entry["size"], entry.get("lfs", {}).get("sha256"))
    required = {"config.json", "processor_config.json", "tokenizer.json", "tokenizer_config.json",
                "chat_template.jinja", "model.safetensors"}
    if not all((directory / name).is_file() for name in required):
        raise ValueError("Required processor/model files are missing")
    (directory / "lubo-model.json").write_text(
        json.dumps({"repo": repo, "revision": revision, "verified": True,
                    "required_files": sorted(required)}, indent=2), encoding="utf-8")
