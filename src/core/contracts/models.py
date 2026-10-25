from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ModelKey:
    model_id: str
    revision: str

    @property
    def token(self):
        return self.model_id + "@" + self.revision

    @classmethod
    def parse(cls, token):
        model_id, revision = token.split("@", 1)
        return cls(model_id, revision)


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str
    url: str
    member: str = ""
    installed_size: int = 0
    installed_sha256: str = ""

    @property
    def digest(self):
        return self.installed_sha256 or self.sha256

    @property
    def disk_size(self):
        return self.installed_size or self.size


@dataclass(frozen=True)
class ModelDescriptor:
    key: ModelKey
    name: str
    capability: str
    adapter: str
    files: tuple[ModelFile, ...]
    source: str
    license: str
    scale: int = 1
    tile: int = 256
    overlap: int = 32
    alignment: int = 1
    legacy_names: tuple[str, ...] = ()
    runtime: str = ""

    @property
    def download_size(self):
        return sum(file.size for file in self.files if file.url)


@dataclass(frozen=True)
class ModelSnapshot:
    descriptor: ModelDescriptor
    state: str
    downloaded: int = 0
    installed_bytes: int = 0
    paths: tuple[tuple[str, str], ...] = ()
    managed: bool = False
    error: str = ""
    default: bool = False
    temporary_bytes: int = 0

    @property
    def usable(self):
        return bool(self.paths) and self.state in ("installed", "bundled", "queued", "downloading", "verifying", "cleaning")


@dataclass(frozen=True)
class ModelLease:
    descriptor: ModelDescriptor
    paths: tuple[tuple[str, str], ...]
