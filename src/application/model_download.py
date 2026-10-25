from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from concurrent.futures import CancelledError
from pathlib import Path
from urllib.request import Request, urlopen


def check_cancelled(cancelled):
    if cancelled():
        raise CancelledError()


def verify_file(path, size, digest, cancelled):
    if path.stat().st_size != size:
        raise ValueError("models.error.size")
    actual = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            check_cancelled(cancelled)
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise ValueError("models.error.checksum")


def download_file(file, path, cancelled, progress):
    offset = path.stat().st_size if path.exists() else 0
    if offset == file.size:
        return
    headers = {"User-Agent": "OpenPictureStudio/ModelManager", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    check_cancelled(cancelled)
    with urlopen(Request(file.url, headers=headers), timeout=20) as response:
        if response.geturl().split(":", 1)[0] != "https":
            raise ValueError("models.error.source")
        if response.status == 206:
            content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if (content_range is None or int(content_range[1]) != offset
                    or int(content_range[3]) != file.size):
                raise ValueError("models.error.range")
        elif response.status == 200:
            offset = 0
        else:
            raise ValueError("models.error.source")
        with path.open("ab" if offset else "wb") as stream:
            progress(offset)
            while chunk := response.read(1 << 18):
                check_cancelled(cancelled)
                offset += len(chunk)
                if offset > file.size:
                    raise ValueError("models.error.size")
                stream.write(chunk)
                progress(offset)


def prepare_file(file, source, target, cancelled):
    verify_file(source, file.size, file.sha256, cancelled)
    if file.member:
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo(file.member)
            if info.file_size != file.disk_size:
                raise ValueError("models.error.size")
            with archive.open(info) as stream, target.open("wb") as output:
                while chunk := stream.read(1 << 20):
                    check_cancelled(cancelled)
                    output.write(chunk)
    else:
        with source.open("rb") as stream, target.open("wb") as output:
            while chunk := stream.read(1 << 20):
                check_cancelled(cancelled)
                output.write(chunk)
    verify_file(target, file.disk_size, file.digest, cancelled)


def check_space(directory: Path, required):
    if shutil.disk_usage(directory).free < required:
        raise ValueError("models.error.space")
