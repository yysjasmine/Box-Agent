"""Stable import facade for model-history helpers."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.context.model_history")
