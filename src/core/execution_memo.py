from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass


current_memo = ContextVar("ops_memo", default=None)
memo_cancellation = ContextVar("ops_memo_cancellation", default=None)


def key_data(value):
    if is_dataclass(value):
        return {field.name: key_data(getattr(value, field.name)) for field in fields(value)
                if field.name != "execution_ownership"}
    if isinstance(value, dict):
        return {key: key_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [key_data(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if hasattr(value, "item"):
        return value.item()
    return value


def buffers(value):
    import numpy as np
    found = {}
    seen = set()
    def visit(item):
        if id(item) in seen:
            return
        seen.add(id(item))
        if isinstance(item, np.ndarray):
            root = item
            while isinstance(root.base, np.ndarray):
                root = root.base
            found[id(root)] = root.nbytes
        elif isinstance(item, memoryview):
            visit(item.obj)
        elif isinstance(item, (bytes, bytearray)):
            found[id(item)] = len(item)
        elif is_dataclass(item):
            for field in fields(item):
                if field.name != "execution_ownership":
                    visit(getattr(item, field.name))
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                visit(child)
    visit(value)
    return found


def freeze(value):
    import numpy as np
    if isinstance(value, np.ndarray):
        value.setflags(write=False)
    elif is_dataclass(value):
        for field in fields(value):
            if field.name != "execution_ownership":
                freeze(getattr(value, field.name))
    elif isinstance(value, dict):
        for item in value.values():
            freeze(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            freeze(item)
    return value


def cancellation_callback(callback):
    return memo_cancellation.get() or callback


def cancelled(callback):
    return cancellation_callback(callback)()


def memo(name, key, load, *, image=False):
    scope = current_memo.get()
    if scope is None:
        return load()
    return scope.memo(name, (scope.input_identity, key) if image else key, load)


@contextmanager
def domain(key):
    scope = current_memo.get()
    if scope is None:
        yield
        return
    previous = scope.domain
    scope.domain = previous, key
    try:
        yield
    finally:
        scope.domain = previous


class ResourceOwner:
    def __init__(self, resources=(), parent=None):
        self.resources = list(resources)
        self.parent = parent

    def release_credit(self, pool):
        return sum(item.consume(pool) for item in self.resources if hasattr(item, "consume"))

    def close(self):
        resources, self.resources = self.resources, []
        for resource in resources:
            resource.close()
        self.parent = None

    def __del__(self):
        self.close()
