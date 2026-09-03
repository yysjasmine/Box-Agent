"""Stable import facade for the context resource ledger."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.context.resource_ledger")
