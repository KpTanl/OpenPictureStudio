"""Frozen application entry for trusted OPS plugin subprocesses."""

from __future__ import annotations

import runpy
import sys


def run(arguments):
    if len(arguments) != 1:
        return 2
    sys.argv = [arguments[0]]
    runpy.run_path(arguments[0], run_name="__main__")
    return 0
