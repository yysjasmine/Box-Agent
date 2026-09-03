"""Compatibility import for the Kernel-owned workflow policy compositor."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.kernel.workflow_composite")
