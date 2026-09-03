"""Stable import facade for the task and artifact registry."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.persistence.task_registry")
