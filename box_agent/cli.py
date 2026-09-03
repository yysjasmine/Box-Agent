"""Stable CLI import facade over the host adapter implementation.

The module alias preserves historical monkeypatch/import behavior while the
implementation is owned by :mod:`box_agent.adapters.cli.app`.
"""

from __future__ import annotations

import sys

from box_agent.adapters.cli import app as _implementation


if __name__ == "__main__":  # pragma: no cover - module execution boundary
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
