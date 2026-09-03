"""Stable import facade for durable workspace metadata."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.persistence.workspace_registry")
