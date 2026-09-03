"""ACP compatibility facade for project context."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.context.project")
