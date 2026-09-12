from tc_agent.plc_cache import PlcSourceCache


def test_cache_snapshot_exposes_metadata_but_not_source_content():
    cache = PlcSourceCache()
    cache.put({"path": r"C:\PLC\MAIN.TcPOU", "content": "secret source", "saved": True})
    snapshot = cache.snapshot()
    assert snapshot["count"] == 1
    assert snapshot["items"][0]["chars"] == len("secret source")
    assert "content" not in snapshot["items"][0]
