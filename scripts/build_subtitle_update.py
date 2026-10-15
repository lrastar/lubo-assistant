"""Build a separate EXE with current project modules, retaining dependencies.

Use Python 3.10 and PyInstaller. The existing onedir EXE is never modified.
--project-modules compiles all local lubo modules into the new entrypoint;
untouched CArchive and PYZ dependencies are verified byte-for-byte. FireRed
sidecars are added only when absent; an existing different support file fails
closed. --protect-exe also guards other named release entrypoints by SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import shutil
import struct
import zlib

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter


def _stage_firered_support(root: Path, app_dir: Path) -> list[tuple[Path, Path]]:
    """Stage worker sidecars without replacing any existing support file."""
    names = ("firered_worker.py", "firered_runtime.py")
    support_dir = app_dir / "quality_support"
    sources = [(root / "scripts" / name, support_dir / name) for name in names]
    for source, target in sources:
        if not source.is_file():
            raise FileNotFoundError(f"Missing FireRed support source: {source}")
        if (target.exists()
                and hashlib.sha256(target.read_bytes()).digest()
                != hashlib.sha256(source.read_bytes()).digest()):
            raise FileExistsError(
                f"Refusing to replace existing support file with different contents: {target}"
            )
    if all(target.exists() for _, target in sources):
        return []

    support_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for source, target in sources:
            if target.exists():
                continue
            temporary = target.with_name(target.name + f".{os.getpid()}.part")
            with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
            staged.append((temporary, target))
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise
    return staged


def rebuild(base: Path, output: Path, main_script: Path | None = None,
            project_modules: bool = False, protected_exes: list[Path] | None = None) -> None:
    if base.resolve() == output.resolve() or output.exists():
        raise ValueError("Output must be a new file; the original EXE is never overwritten")
    if base.parent.resolve() != output.parent.resolve():
        raise ValueError("The new EXE must share the base onedir runtime directory")
    protected_hashes: dict[Path, bytes] = {}
    for protected in protected_exes or []:
        protected = protected.resolve()
        if protected == output.resolve():
            raise ValueError(f"The output cannot be a protected EXE: {protected}")
        if not protected.is_file():
            raise FileNotFoundError(f"Protected EXE does not exist: {protected}")
        protected_hashes[protected] = hashlib.sha256(protected.read_bytes()).digest()
    original = base.read_bytes()
    archive = CArchiveReader(str(base))
    if archive._end_offset != len(original):
        raise ValueError("Unsupported archive trailer; use a full source build instead")
    pyz = archive.extract("PYZ.pyz")
    if pyz[4:8] != importlib.util.MAGIC_NUMBER:
        raise ValueError("Build with the same Python bytecode version as the bundled EXE")
    toc_offset = struct.unpack("!i", pyz[8:12])[0]
    toc = dict(marshal.loads(pyz[toc_offset:]))
    if "lubo.asr" not in toc:
        raise ValueError("This is not a compatible LuboStudio bundle")
    root = Path(__file__).resolve().parent.parent
    source = root / "lubo" / "asr.py"
    replacement = compile(source.read_bytes(), "lubo/asr.py", "exec", dont_inherit=True)
    replacement_bytes = marshal.dumps(replacement)
    replacements = {"lubo.asr": (0, replacement_bytes)}
    if project_modules:
        for path in sorted((root / "lubo").rglob("*.py")):
            relative = path.relative_to(root)
            parts = list(relative.with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            name = ".".join(parts)
            replacements[name] = (int(is_package), marshal.dumps(
                compile(path.read_bytes(), relative.as_posix(), "exec", dont_inherit=True)))
    entrypoint = (marshal.dumps(compile(main_script.read_bytes(), main_script.name, "exec",
                                       dont_inherit=True)) if main_script else None)

    data_start = min(pos for _, pos, size in toc.values() if size)
    rebuilt_pyz = bytearray(pyz[:data_start])
    new_toc = []
    for name, (kind, pos, size) in toc.items():
        blob = (zlib.compress(replacements[name][1], 6) if name in replacements
                else pyz[pos:pos + size])
        new_toc.append((name, (kind, len(rebuilt_pyz), len(blob))))
        rebuilt_pyz.extend(blob)
    for name, (kind, payload) in replacements.items():
        if name not in toc:
            blob = zlib.compress(payload, 6)
            new_toc.append((name, (kind, len(rebuilt_pyz), len(blob))))
            rebuilt_pyz.extend(blob)
    rebuilt_pyz[8:12] = struct.pack("!i", len(rebuilt_pyz))
    rebuilt_pyz.extend(marshal.dumps(new_toc))

    data = bytearray()
    entries = []
    for name, (pos, size, uncompressed_size, compressed, kind) in archive.toc.items():
        if name == "PYZ.pyz":
            uncompressed_size = len(rebuilt_pyz)
            blob = zlib.compress(rebuilt_pyz, 6) if compressed else rebuilt_pyz
        elif name == "main" and entrypoint is not None:
            uncompressed_size = len(entrypoint)
            blob = zlib.compress(entrypoint, 6) if compressed else entrypoint
        else:
            start = archive._start_offset + pos
            blob = original[start:start + size]
        entries.append((len(data), len(blob), uncompressed_size, compressed, kind, name))
        data.extend(blob)
    for option in archive.options:
        entries.append((0, 0, 0, 0, "o", option))
    toc_pos = len(data)
    table = CArchiveWriter._serialize_toc(entries)
    data.extend(table)
    cookie_start = archive._end_offset - archive._COOKIE_LENGTH
    magic, _, _, _, pyver, pylib = struct.unpack(archive._COOKIE_FORMAT, original[cookie_start:])
    data.extend(struct.pack(archive._COOKIE_FORMAT, magic,
                            len(data) + archive._COOKIE_LENGTH, toc_pos, len(table), pyver, pylib))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    with temporary.open("xb") as stream:
        stream.write(original[:archive._start_offset])
        stream.write(data)
    staged_support: list[tuple[Path, Path]] = []
    published_support: list[tuple[Path, bytes]] = []
    source_hashes: dict[Path, bytes] = {}
    try:
        check = CArchiveReader(str(temporary))
        assert check.options == archive.options
        assert set(check.toc) == set(archive.toc)
        for name in archive.toc:
            if name == "main" and entrypoint is not None:
                assert check.extract(name) == entrypoint
            elif name != "PYZ.pyz":
                assert check.extract(name) == archive.extract(name), name
        new_pyz = check.extract("PYZ.pyz")
        checked = check.open_embedded_archive("PYZ.pyz")
        assert set(checked.toc) == set(toc) | set(replacements)
        for name, (_, pos, size) in toc.items():
            if name not in replacements:
                _, new_pos, new_size = checked.toc[name]
                assert pyz[pos:pos + size] == new_pyz[new_pos:new_pos + new_size], name
        for name, (kind, payload) in replacements.items():
            _, pos, size = checked.toc[name]
            assert zlib.decompress(new_pyz[pos:pos + size]) == payload, name
            assert checked.extract(name).co_name == "<module>", name
        assert hashlib.sha256(base.read_bytes()).digest() == hashlib.sha256(original).digest()
        for protected, expected_hash in protected_hashes.items():
            if hashlib.sha256(protected.read_bytes()).digest() != expected_hash:
                raise RuntimeError(f"Protected EXE changed while building: {protected}")
        root = Path(__file__).resolve().parent.parent
        sources = [root / "scripts" / name
                   for name in ("firered_worker.py", "firered_runtime.py")]
        source_hashes = {source: hashlib.sha256(source.read_bytes()).digest() for source in sources}
        staged_support = _stage_firered_support(root, base.parent)
        for staged, target in staged_support:
            staged.rename(target)
            published_support.append((target, source_hashes[root / "scripts" / target.name]))
        temporary.rename(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        for staged, _ in staged_support:
            staged.unlink(missing_ok=True)
        for target, expected_hash in published_support:
            if target.exists() and hashlib.sha256(target.read_bytes()).digest() == expected_hash:
                target.unlink(missing_ok=True)
        raise
    print(f"Built: {output}")
    print(f"Original SHA256: {hashlib.sha256(original).hexdigest()}")
    print(f"Updated SHA256: {hashlib.sha256(output.read_bytes()).hexdigest()}")
    print(f"Verified {len(set(toc) - set(replacements))} unchanged modules; compiled {len(replacements)} source modules")
    for protected in protected_hashes:
        print(f"Protected SHA256 unchanged: {protected} ({hashlib.sha256(protected.read_bytes()).hexdigest()})")
    print(f"FireRed sidecars: {base.parent / 'quality_support' / 'firered_worker.py'}; "
          f"{base.parent / 'quality_support' / 'firered_runtime.py'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--main-script", type=Path, help="Optional standalone worker entrypoint")
    parser.add_argument("--project-modules", action="store_true", help="Include all current lubo modules")
    parser.add_argument("--protect-exe", type=Path, action="append", default=[],
                        help="Existing EXE whose SHA-256 must remain unchanged; may be repeated")
    args = parser.parse_args()
    rebuild(args.base, args.output, args.main_script, args.project_modules, args.protect_exe)
