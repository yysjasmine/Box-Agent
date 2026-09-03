"""Stable import facade for task identity context."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.context.task")
