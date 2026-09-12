from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tc_agent import agent_core, config
from tc_agent.backend import _system_prompt


ROOT = Path(__file__).resolve().parents[1]


def _review(rule: str, severity: str = "warning") -> dict:
    finding = {
        "rule": rule,
        "severity": severity,
        "object": "FB_Demo",
        "message": "test finding",
    }
    return {"approved": False, "blocking_findings": [finding], "advisories": []}


def test_quality_gate_defaults_on_and_is_public() -> None:
    migrated = config._migrate({})
    assert migrated["quality_gate_enabled"] is True
    assert config.public_settings(migrated)["quality_gate_enabled"] is True


def test_disabled_gate_bypasses_soft_quality_finding() -> None:
    review = _review("fb-status-quad")
    with patch("tc_agent.config.load_config", return_value={"quality_gate_enabled": False}):
        assert agent_core._quality_review_blocks(review) is False
    assert review["approved"] is True
    assert review["gate"] == "bypassed"


def test_disabled_gate_never_bypasses_error_or_interface_structure() -> None:
    for review in (
        _review("TCSA0040", "error"),
        _review("interface-member-inline", "warning"),
    ):
        with patch("tc_agent.config.load_config", return_value={"quality_gate_enabled": False}):
            assert agent_core._quality_review_blocks(review) is True
        assert review["gate"] == "hard-blocked"


def test_prompt_and_webview_expose_quality_gate_state() -> None:
    assert "PLC 代码质量门禁已启用" in _system_prompt("demo.sln", quality_gate_enabled=True)
    disabled = _system_prompt("demo.sln", quality_gate_enabled=False)
    assert "PLC 代码质量门禁已关闭" in disabled
    assert "硬门禁" in disabled
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="gateToggle"' in html
    assert 'type: "set_quality_gate"' in html
    assert 'case "quality_gate_changed"' in html
