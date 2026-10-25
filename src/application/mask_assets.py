from __future__ import annotations

import hashlib
import io
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

from ..core.mask_components import (asset_references, composite_weight,
                                     geometry_hash, prepare_components)
from ..core.project.project_store import atomic_write
from ..core.project.assets import ProjectAssetError, validate_asset


def workflow_assets(workflow):
    result = {}
    for node in workflow.nodes:
        settings = node.params.get("settings", {}) if node.node_type == "tool" else node.params
        if node.node_type == "color_adjust" or node.node_type == "tool" and node.params.get("tool_id") == "color_adjust":
            for digest, entry in asset_references(settings).items():
                if digest in result and result[digest] != entry:
                    raise ValueError("error.mask.asset")
                result[digest] = entry
    return result


class MaskAssetStore:
    def __init__(self, resources=None, directory=None):
        from .storage_paths import data_root
        self.directory = Path(directory) if directory is not None else data_root() / "mask_assets"
        self.resources = resources

    def path(self, digest):
        import re
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("error.mask.asset")
        return self.directory / (digest + ".png")

    def put(self, weights, cancelled):
        if (weights.ndim != 2 or not 1 <= min(weights.shape) <= max(weights.shape) <= 1600
                or not np.isfinite(weights).all()):
            raise ValueError("error.mask.asset")
        if cancelled():
            raise CancelledError()
        with Image.fromarray(np.rint(np.clip(weights, 0, 1) * 65535).astype(np.uint16)) as image, io.BytesIO() as stream:
            image.save(stream, format="PNG")
            payload = stream.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.path(digest)
        try:
            validate_asset(destination, digest, (weights.shape[1], weights.shape[0]), cancelled)
        except ProjectAssetError:
            atomic_write(destination, payload, is_cancelled=cancelled)
        validate_asset(destination, digest, (weights.shape[1], weights.shape[0]), cancelled)
        return digest

    def import_asset(self, path, digest, size, cancelled):
        from ..core.project.project_store import ProjectStore
        validate_asset(path, digest, size, cancelled)
        destination = self.path(digest)
        if Path(path).resolve() != destination.resolve():
            try:
                ProjectStore().copy_artifact(Path(path), destination, digest, is_cancelled=cancelled, replace_corrupt=True)
            except (OSError, ValueError) as error:
                raise ProjectAssetError(path) from error

    @contextmanager
    def borrow(self, digest, size, cancelled):
        path = self.path(digest)
        try:
            stat = path.stat()
        except OSError as error:
            raise ProjectAssetError(path) from error
        key = ("mask-asset", str(path.resolve()), digest, tuple(size), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        def load(abandoned):
            validate_asset(path, digest, size, abandoned)
            try:
                with Image.open(path) as image:
                    value = np.asarray(image, dtype=np.float32) / np.float32(65535)
            except OSError as error:
                raise ProjectAssetError(path) from error
            value.setflags(write=False)
            return value
        if self.resources is None:
            yield load(cancelled)
        else:
            with self.resources.acquire(key, "preview", size[0] * size[1] * 16 + 1024 * 1024,
                    load, lambda value: value.nbytes, cancelled) as lease:
                yield lease.value

    @contextmanager
    def prepare(self, source, params, cancelled, *, active_only=True):
        shape, reference_size = source.pixels.shape[:2], source.orig_size
        with self.prepare_size(params, shape, reference_size, cancelled, active_only=active_only) as masks:
            yield masks

    @contextmanager
    def prepare_size(self, params, shape, reference_size, cancelled, *, active_only=True, identities=None):
        masks = prepare_components(params.get("masks"), active_only=active_only)
        if identities is not None:
            masks = tuple(mask for mask in masks if mask.identity in identities)
        with ExitStack() as stack:
            assets = {}
            for mask in masks:
                for component in mask.components:
                    if "asset" in component and component["asset"] not in assets:
                        assets[component["asset"]] = stack.enter_context(self.borrow(component["asset"], component["size"], cancelled))
            result = []
            for mask in masks:
                key = ("mask-weight", geometry_hash(mask), shape, tuple(reference_size), 1)
                def load(abandoned, item=mask):
                    memory = self.resources.working_memory(shape[1] * min(64, shape[0]) * 4 * 32, abandoned) if self.resources else nullcontext()
                    with memory:
                        value = np.empty(shape, np.float32)
                        for start in range(0, shape[0], 64):
                            stop = min(start + 64, shape[0])
                            value[start:stop] = composite_weight(item, shape, start, stop, reference_size, assets, abandoned)
                        value.setflags(write=False)
                        return value
                if self.resources is None:
                    raster = load(cancelled)
                else:
                    lease = stack.enter_context(self.resources.acquire(key, "preview", shape[0] * shape[1] * 4,
                        load, lambda value: value.nbytes, cancelled))
                    raster = lease.value
                result.append(replace(mask, raster=raster))
            yield tuple(result)
