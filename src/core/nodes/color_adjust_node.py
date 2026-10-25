from __future__ import annotations

from ..photo_engine import render_source


def execute(source, params, *, threads=None, cancelled=lambda: False, sampling=None):
    return render_source(source, params, threads=threads, cancelled=cancelled, sampling=sampling)
