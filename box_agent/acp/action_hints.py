"""ACP compatibility facade for Context Engine action hints."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.context.action_hints")
