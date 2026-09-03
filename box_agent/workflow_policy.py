"""Stable import facade for the workflow plugin contract."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.workflows.contract")
