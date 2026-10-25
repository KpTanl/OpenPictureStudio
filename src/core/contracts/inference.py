from dataclasses import dataclass

CPU = "cpu"
DIRECTML = "directml"


@dataclass(frozen=True)
class BackendStatus:
    preference: str
    backend: str
    reason: str = ""


@dataclass(frozen=True)
class RuntimeFile:
    python: str
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class RuntimeDescriptor:
    runtime_id: str
    version: str
    name: str
    backend: str
    source: str
    license: str
    package: str
    installed_size: int
    files: tuple[RuntimeFile, ...]

    @property
    def token(self):
        return self.runtime_id + "@" + self.version

    def file_for(self, python):
        return next((file for file in self.files if file.python == python), None)


@dataclass(frozen=True)
class RuntimeSnapshot:
    descriptor: RuntimeDescriptor
    state: str
    downloaded: int = 0
    installed_bytes: int = 0
    path: str = ""
    active: bool = False
    enabled: bool = False
    error: str = ""


@dataclass(frozen=True)
class GraphicsAdapter:
    name: str
    vendor_id: int
    dedicated_bytes: int
    directx12: bool


@dataclass(frozen=True)
class MemoryStatus:
    """读取那一刻的物理内存：总量与可用量（字节）；已用是两者之差。"""

    total_bytes: int
    available_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.available_bytes


@dataclass(frozen=True)
class SystemProfile:
    """本机硬件：处理器名称与核数、物理内存总量、显卡及其显存。内存的实时占用另由 memory_status() 读取。"""

    cpu: str = ""
    logical_processors: int = 0
    memory_bytes: int = 0
    adapters: tuple[GraphicsAdapter, ...] = ()
    error: str = ""
    physical_cores: int = 0


@dataclass(frozen=True)
class Advice:
    message_key: str
    action: str = ""
    params: tuple[tuple[str, str], ...] = ()
