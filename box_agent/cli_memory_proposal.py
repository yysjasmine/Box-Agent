"""Stable import facade for the CLI memory proposal adapter."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.adapters.cli.memory_proposal_impl")
