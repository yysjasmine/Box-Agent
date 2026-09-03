"""Stable import facade for :mod:`box_agent.workflows.completion`."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.workflows.completion")
