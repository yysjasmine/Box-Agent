from __future__ import annotations

import importlib.util
from pathlib import Path


def test_repository_scripts_package_wins_over_environment_packages() -> None:
    spec = importlib.util.find_spec("scripts")

    assert spec is not None
    assert spec.origin is not None
    assert Path(spec.origin).resolve() == (
        Path(__file__).resolve().parents[1] / "scripts" / "__init__.py"
    )
