from .result_types import CoreError, CoreResult, ErrorCode

__all__ = ["CoreAPI", "CoreError", "CoreResult", "ErrorCode", "CommitPolicy", "CommitResult",
           "ExportRequest", "ExportResult", "ImageEngineAdapter", "LegacyEngineAdapter",
           "PreviewResult", "ProjectSession"]


def __getattr__(name):
    if name == "CoreAPI":
        from .core_api import CoreAPI
        return CoreAPI
    if name in __all__:
        from . import services
        return getattr(services, name)
    raise AttributeError(name)
