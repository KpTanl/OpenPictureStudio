from ..core.contracts.result import SourceChangedError
from ..core.contracts.gpu import GpuExecutionError


class DeferredSource:
    def __init__(self, metadata, loader, input_loader=None):
        self.metadata, self.loader = metadata, loader
        self.input_loader = input_loader
        self._lease = None

    def __getattr__(self, name):
        return getattr(self.metadata, name)

    def materialize(self, cancelled):
        if self._lease is None:
            lease = self.loader(cancelled)
            if (lease.value.content_identity != self.metadata.content_identity
                    or lease.value.pixels.shape != self.metadata.pixels.shape
                    or lease.value.orig_size != self.metadata.orig_size
                    or lease.value.is_proxy != self.metadata.is_proxy
                    or lease.value.source_format != self.metadata.source_format
                    or lease.value.color_pipeline != self.metadata.color_pipeline
                    or (lease.value.alpha is None) != (self.metadata.alpha is None)):
                lease.close()
                raise SourceChangedError()
            self._lease = lease
        return self._lease.value

    def resolve(self, cancelled, prepare=None):
        if prepare is not None and self.input_loader is not None:
            lease = self.input_loader(cancelled)
            if lease is not None:
                with lease:
                    return prepare(lease, self.metadata, cancelled)
            raise GpuExecutionError("source_input", "Planned integer source input is unavailable")
        return self.materialize(cancelled)

    def close(self):
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.close()


class DeferredSourceLease:
    def __init__(self, metadata, loader, input_loader=None):
        self.value = DeferredSource(metadata, loader, input_loader)

    def close(self):
        self.value.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
