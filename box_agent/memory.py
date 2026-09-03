"""Stable import facade for :mod:`box_agent.memory_engine.store`."""

from __future__ import annotations

import sys

from box_agent.memory_engine import store as _implementation


sys.modules[__name__] = _implementation
