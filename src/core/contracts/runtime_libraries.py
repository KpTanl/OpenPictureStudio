from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeLibrarySnapshot:
    runtime: str
    state: str
    source: str
    token: str = ""
    version: str = ""
    installed_bytes: int = 0
    error: str = ""

    @property
    def available(self):
        return self.state == "available"
