"""Stable import facade for the Agent Service implementation."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.services.kernel")
