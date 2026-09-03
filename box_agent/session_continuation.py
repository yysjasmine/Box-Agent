"""Stable import facade for durable session continuation."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.persistence.session_continuation")
