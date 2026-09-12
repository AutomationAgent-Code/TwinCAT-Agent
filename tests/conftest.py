"""Common pytest classification for the TwinCAT Agent test suite.

The default suite is intentionally offline and mock-based.  Environment-specific
tests opt in by filename so CI can select a safe layer without relying on
individual test authors to repeat marker declarations.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply one stable, visible test layer to every collected test item."""

    for item in items:
        relative = Path(str(item.fspath)).as_posix().lower()
        item.add_marker(pytest.mark.unit)

        if "ui" in relative or "webview" in relative or "license_status" in relative:
            item.add_marker(pytest.mark.ui)
        if "_xae4024" in relative:
            item.add_marker(pytest.mark.xae4024)
        if "_xae4026" in relative:
            item.add_marker(pytest.mark.xae4026)
        if any(token in relative for token in ("deploy", "runtime", "nc_tools", "agent_tools")):
            item.add_marker(pytest.mark.integration)
        if any(token in relative for token in ("_hardware", "ethercat")):
            item.add_marker(pytest.mark.hardware)
        if "_destructive" in relative:
            item.add_marker(pytest.mark.destructive)
