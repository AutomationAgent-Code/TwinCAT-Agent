from __future__ import annotations

from unittest.mock import patch

from tc_agent import backend


def test_prewarm_caches_active_unsaved_member():
    live = {
        "name": "MAIN",
        "method": "Run",
        "implementation": "nValue := 1;",
        "active_document": {
            "source_file": r"C:\PLC\POUs\MAIN.TcPOU",
            "member": "Run",
            "saved": False,
        },
    }
    with patch.object(backend.ac, "_plc_read_current", return_value=live), \
            patch.object(backend.ac, "ps_com", return_value={"solution": "Machine.sln"}):
        cached = backend._prewarm_active_plc_cache(12, "Machine.sln")
    assert cached is not None
    assert cached["name"] == "MAIN"
    assert cached["member"] == "Run"
    assert cached["saved"] is False


def test_prewarm_ignores_when_no_active_plc_document():
    with patch.object(backend.ac, "_plc_read_current", side_effect=ValueError("no document")):
        assert backend._prewarm_active_plc_cache() is None
