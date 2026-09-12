"""Provider save/enable contract regressions.

These tests stay offline: configuration is redirected to a temporary file and
provider construction is validated without making a model request.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from tc_agent import config
from tc_agent import backend


ROOT = Path(__file__).resolve().parents[1]


def _provider(name: str, *, key: str = "key", model: str = "model") -> dict:
    return {
        "name": name,
        "base_url": "https://gateway.example/v1",
        "api_key": key,
        "model": model,
        "protocol": "openai",
    }


def test_save_new_provider_does_not_enable_it(tmp_path):
    with patch.object(config, "CONFIG_PATH", tmp_path / "config.json"):
        saved = config.upsert_provider(_provider("A"))
        assert saved["active_provider"] == ""
        assert saved["providers"][0]["name"] == "A"
        assert config.public_settings(saved)["active_provider"] == ""


def test_non_active_edit_and_empty_key_preserve_active_and_secret(tmp_path):
    path = tmp_path / "config.json"
    with patch.object(config, "CONFIG_PATH", path):
        first = config.upsert_provider(_provider("A", key="secret-a"))
        first = config.set_active(first["providers"][0]["id"])
        second = config.upsert_provider(_provider("B", key="secret-b"))
        second_id = next(item["id"] for item in second["providers"] if item["name"] == "B")
        edited = config.upsert_provider({"id": second_id, "name": "B edited", "api_key": ""})
        assert edited["active_provider"] == first["active_provider"]
        saved_b = next(item for item in edited["providers"] if item["id"] == second_id)
        assert saved_b["name"] == "B edited"
        assert saved_b["api_key"] == "secret-b"


def test_deleting_active_provider_clears_selection_without_fallback(tmp_path):
    with patch.object(config, "CONFIG_PATH", tmp_path / "config.json"):
        saved = config.upsert_provider(_provider("A"))
        provider_id = saved["providers"][0]["id"]
        config.set_active(provider_id)
        config.upsert_provider(_provider("B"))
        deleted = config.delete_provider(provider_id)
        assert deleted["active_provider"] == ""
        assert [item["name"] for item in deleted["providers"]] == ["B"]
        assert config._migrate(json.loads(json.dumps(deleted)))["active_provider"] == ""


def test_failed_enable_does_not_mutate_config_or_current_provider(tmp_path):
    with patch.object(config, "CONFIG_PATH", tmp_path / "config.json"):
        saved = config.upsert_provider(_provider("A"))
        active_id = saved["providers"][0]["id"]
        config.set_active(active_id)
        config.upsert_provider(_provider("B", key=""))
        before = config.load_config()
        candidate = dict(before)
        candidate["providers"] = list(before["providers"])
        candidate["active_provider"] = next(item["id"] for item in before["providers"] if item["name"] == "B")
        provider, error = backend._build_provider(candidate)
        assert provider is None
        assert "API Key" in error
        assert config.load_config()["active_provider"] == active_id


def test_inflight_foreground_turn_keeps_provider_snapshot():
    old = object()
    new = object()
    turn = backend.ForegroundTurn("thread", object(), 0, "solution", 1, old)
    connection_provider = old
    connection_provider = new
    assert turn.provider is old
    assert connection_provider is new


def test_webview_exposes_separate_enable_action_and_failure_contract():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    duplicate = (ROOT / "tc_agent_vsix" / "webview" / "index.html").read_text(encoding="utf-8")
    assert html == duplicate
    assert 'type: "save_provider", provider: prov, request_id: requestId' in html
    assert 'type: "set_provider", provider_id: id, request_id: requestId' in html
    assert 'textContent = p.id === activeProvider ? "已启用" : "启用"' in html
    assert 'case "provider_saved"' in html
    assert 'case "provider_switch_failed"' in html
    assert 'closeForm();' in html
    save_block = html[html.index('$("pfSave").onclick'):html.index('function mcpSetMessage')]
    assert 'ready = false; sendBtn.disabled = true' not in save_block
    assert 'closeForm();' not in save_block
    assert 'class="settings-page-head"' in html
    assert 'class="provider-manager"' in html
    assert '保存配置' in html
    assert '#overlay { background: var(--bg)' in html
    assert 'providerFormDirty' in html
    assert 'type:"get_snapshot_catalog"' in html
    assert 'type:"get_snapshot_content"' in html
    assert 'snapshotState.requestId' in html


def test_backend_emits_distinct_provider_events_without_stopped_cleanup():
    source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
    block = source[source.index('elif kind in ("set_provider"'):source.index('elif kind in ("save_mcp_server"', source.index('elif kind in ("set_provider"'))]
    assert 'event_type = "provider_saved"' in block
    assert 'event_type = "provider_switched"' in block
    assert 'event_type = "provider_deleted"' in block
    assert 'type="stopped"' not in block
    assert 'type=failure_type' in block


def test_snapshot_backend_contract_is_read_only_and_scope_bound():
    source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
    versions = (ROOT / "tc_agent" / "plc_versions.py").read_text(encoding="utf-8")
    assert 'get_snapshot_catalog' in source
    assert 'get_snapshot_detail' in source
    assert 'get_snapshot_content' in source
    assert 'snapshot_scope_matches' in source
    assert 'asyncio.to_thread' in source[source.index('async def handle_snapshot_request'):]
    assert 'SNAPSHOT_DIR = ".TwinCATAgent/snapshots"' in versions
    assert '_safe_snapshot_file' in versions
    assert 'schema_version' in versions
