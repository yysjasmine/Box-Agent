"""Stable import facade for durable workflow checkpoints."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("box_agent.persistence.workflow_checkpoint_store")
