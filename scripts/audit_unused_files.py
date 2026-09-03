"""Audit disposable files without making destructive changes.

The command emits candidates; a maintainer must inspect the list before
deleting anything. Source modules are only classified when they are explicitly
known generated artifacts, so an incomplete static graph fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".txt",
}
PROTECTED_PREFIXES = (
    "box_agent/compat",
    "box_agent/agent.py",
    "box_agent/core.py",
    "box_agent/cli.py",
    "box_agent/acp",
    "box_agent/runtime.py",
    "tests",
    "docs",
    "scripts",
    "pyproject.toml",
)
PROTECTED_EXACT = {
    "tests/e2e/report.json",
}
DISPOSABLE_NAMES = {"output.png", "Thumbs.db", ".DS_Store"}
DISPOSABLE_SUFFIXES = {".bak", ".tmp", ".old"}
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp-tests",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "workspace",
}
SKIP_PREFIXES = ("box_agent/skills/", "docs/assets/")
REFERENCE_EXCLUDES = {"scripts/audit_unused_files.py", "tests/test_file_hygiene.py"}


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_protected(relative: str) -> bool:
    return relative in PROTECTED_EXACT or any(
        relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in PROTECTED_PREFIXES
    )


def _files(root: Path) -> list[Path]:
    # Prune ignored directories before descending.  ``Path.rglob`` visits all
    # of ``workspace/`` first, which made the audit unexpectedly scan large
    # runtime archives and appear hung after E2E/build runs.
    paths: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        current = Path(directory)
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in SKIP_PARTS and not name.startswith(".venv-")
        ]
        for filename in filenames:
            path = current / filename
            relative = _relative(path, root)
            if relative in REFERENCE_EXCLUDES:
                continue
            if any(relative.startswith(prefix) for prefix in SKIP_PREFIXES):
                continue
            paths.append(path)
    return sorted(paths)


def _references(root: Path, files: list[Path]) -> dict[str, int]:
    corpus: list[str] = []
    for path in files:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            corpus.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    text = "\n".join(corpus)
    counts: dict[str, int] = {}
    for path in files:
        relative = _relative(path, root)
        if path.suffix.lower() in TEXT_SUFFIXES:
            counts[relative] = text.count(relative) + text.count(relative.replace("/", "\\"))
        else:
            counts[relative] = text.count(relative) + text.count(path.name)
    return counts


def audit(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    files = _files(root)
    references = _references(root, files)
    candidates: list[dict[str, Any]] = []
    for path in files:
        relative = _relative(path, root)
        if _is_protected(relative):
            continue
        generated = path.name in DISPOSABLE_NAMES or path.suffix.lower() in DISPOSABLE_SUFFIXES
        if not generated:
            continue
        reference_count = references.get(relative, 0)
        candidates.append(
            {
                "path": relative,
                "safe_to_delete": reference_count == 0,
                "reason": "generated/disposable artifact with no textual references"
                if reference_count == 0
                else "generated/disposable artifact still referenced",
                "reference_count": reference_count,
            }
        )
    return {
        "schema_version": 1,
        "root": str(root),
        "scanned_files": len(files),
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit unused disposable files")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", dest="json_path", type=Path)
    args = parser.parse_args()
    report = audit(args.root)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["audit", "main"]
