"""Stable import facade for the CLI permission adapter."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.adapters.cli.permission_broker_impl")
