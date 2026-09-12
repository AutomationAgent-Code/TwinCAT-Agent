"""TwinCAT Template Manager public package metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _read_version() -> str:
    """Use VERSION in a checkout and wheel metadata after installation."""
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    try:
        return version("tc-template")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _read_version()
