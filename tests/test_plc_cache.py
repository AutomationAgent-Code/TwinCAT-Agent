from __future__ import annotations

from unittest.mock import patch

from tc_agent import agent_core
from tc_agent.plc_cache import PlcSourceCache, cache_scope


def test_cache_returns_unique_current_document_by_object_and_member():
    cache = PlcSourceCache()
    cache.put({"path": r"C:\PLC\POUs\MAIN.TcPOU", "implementation": "x := 1;", "saved": False,
               "xae_pid": 12, "solution": "Machine.sln"})
    with cache_scope(12, "Machine.sln"):
        result = cache.get("MAIN", area="implementation")
    assert result is not None
    assert result["source"] == "xae_cache"
    assert result["implementation"] == "x := 1;"
    assert result["cache_saved"] is False


def test_plc_read_prefers_extension_cache_before_com():
    cached = {"name": "MAIN", "source": "xae_cache", "implementation": "x := 1;"}
    with patch.object(agent_core.PLC_SOURCE_CACHE, "get", return_value=cached), \
         patch.object(agent_core, "ps_com") as com:
        assert agent_core._plc_read({"name": "MAIN", "area": "implementation"}) == cached
    com.assert_not_called()


def test_plc_read_smart_forces_com_when_live_is_requested():
    cached = {"name": "MAIN", "source": "xae_cache", "implementation": "cached"}
    with patch.object(agent_core.PLC_SOURCE_CACHE, "get", return_value=cached), \
         patch.object(agent_core, "ps_com", return_value={"name": "MAIN", "implementation": "live"}) as com:
        result = agent_core._plc_read_smart({"name": "MAIN", "live": True})
    assert result["source"] == "live_com"
    com.assert_called_once()
