from pathlib import Path

from .image_output_service import BatchOutputResult
from .panel_render_service import PanelRenderService
from .result_types import CoreOperationError


class EnhancementBatchService:
    def __init__(self, runtime, sources, outputs):
        self.runtime = runtime
        self.sources = sources
        self.outputs = outputs
        self.renderer = PanelRenderService(runtime)

    def export(self, paths, params, directory, *, extension=".png", cancellation):
        result = BatchOutputResult()
        reference = self.renderer.prepare("ai_enhance", params)
        generation = self.runtime.generation
        cancelled = lambda: cancellation.is_cancelled() or not self.runtime.is_current(generation)
        for path in paths:
            if cancelled():
                result.cancelled.append(path)
                continue
            source = None
            try:
                identity = self.sources.identity(path, is_cancelled=cancelled)
                source = self.renderer.render_source(identity, reference.tool_id, params,
                    source_service=self.sources, is_cancelled=cancelled)
                if cancelled():
                    result.cancelled.append(path)
                    continue
                base = Path(directory) / (Path(path).stem + "_enhanced")
                target = Path(str(base) + extension)
                ordinal = 1
                while target.exists() or str(target) in result.succeeded:
                    target = Path(str(base) + f"-{ordinal}" + extension)
                    ordinal += 1
                self.outputs.save(source, str(target), overwrite=False, cancellation=cancellation,
                                  publication=lambda: self._publication(generation, cancellation))
                result.succeeded.append(str(target))
            except Exception as error:
                if cancelled():
                    result.cancelled.append(path)
                else:
                    key = error.error.message_key if isinstance(error, CoreOperationError) else str(error)
                    result.failed.append((path, key))
            finally:
                source = None
        return result

    def _publication(self, generation, cancellation):
        from contextlib import contextmanager

        @contextmanager
        def publish():
            from types import SimpleNamespace
            with self.runtime.publication(SimpleNamespace(generation=generation)) as active, cancellation.publication() as valid:
                yield active and valid
        return publish()
