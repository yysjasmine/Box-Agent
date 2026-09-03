"""Stable import facade for Tool result persistence."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.tools.result_storage")
