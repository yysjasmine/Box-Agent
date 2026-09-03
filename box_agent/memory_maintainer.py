"""Stable import facade for :mod:`box_agent.memory_engine.maintenance`."""

from __future__ import annotations

import sys

from box_agent.memory_engine import maintenance as _implementation


sys.modules[__name__] = _implementation
