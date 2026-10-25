from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PureWindowsPath
import re
import stat

MANIFEST_NAME = "install-manifest.txt"


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _files(directory):
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        if _linked(info):
            raise ValueError(f"Linked bundle entry: {path}")
        if stat.S_ISDIR(info.st_mode):
            yield from _files(path)
        elif stat.S_ISREG(info.st_mode):
            yield path, info
        else:
            raise ValueError(f"Not a regular bundle file: {path}")


def build_manifest(directory, version, source_sha):
    if not re.fullmatch(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}", version):
        raise ValueError("Invalid application version")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        raise ValueError("A complete Git commit SHA is required")
    directory = Path(directory).absolute()
    for path in (directory, *directory.parents):
        if _linked(path.lstat()):
            raise ValueError(f"Linked program directory: {path}")
    if not (directory / "OpenPictureStudio.exe").is_file():
        raise ValueError("OpenPictureStudio.exe is missing")
    lines = ["OPS-INSTALL\t1", "version\t" + version, "source\t" + source_sha.lower()]
    seen = set()
    for path, info in _files(directory):
        relative = str(PureWindowsPath(path.relative_to(directory)))
        if relative == MANIFEST_NAME:
            continue
        if (relative.lower() in seen or any(ord(char) < 32 for char in relative)
                or any(part.endswith((".", " ")) or ":" in part for part in PureWindowsPath(relative).parts)
                or relative.lower().startswith((".install-", "unins"))):
            raise ValueError(f"Invalid bundle path: {relative}")
        seen.add(relative.lower())
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Bundle changed while hashing: {relative}")
        lines.append(f"{digest.hexdigest()}\t{info.st_size}\t{relative}")
    target = directory / MANIFEST_NAME
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    arguments = parser.parse_args()
    print(build_manifest(arguments.directory, arguments.version, arguments.source_sha))
