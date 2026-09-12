"""Offline regressions for cancellation, source identity and verification truth."""
import ast
import asyncio
import contextlib
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tc_agent import agent_core as ac, backend
from tc_agent.execution_policy import ReadPolicy, tool_succeeded, saved_read
from tc_agent.plc_cache import PlcSourceCache, cache_scope
from tc_agent.plc_source import discover_projects


def test_actual_dispatch_cannot_run_queued_write_after_cancel():
    # Compile the real nested dispatcher with isolated dependencies, not a copy.
    tree = ast.parse(Path(backend.__file__).read_text(encoding="utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_tool")
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[]))
    lock, queued, finished, called = threading.RLock(), threading.Event(), threading.Event(), threading.Event()

    @contextlib.contextmanager
    def target_lock(pid):
        queued.set()
        try:
            with lock:
                yield
        finally:
            finished.set()

    namespace = dict(vars(backend), conversation_store=None, last_solution="Fixture.sln", turn_read_cache={},
                     saved_read=lambda *a: False, _tool_readonly=lambda *a: False,
                     _tool_category=lambda *a: "代码", _xae_tool_lock=target_lock,
                     PLC_SOURCE_CACHE=PlcSourceCache(),
                     MCP_MANAGER=SimpleNamespace(has_tool=lambda *a: False),
                     ac=SimpleNamespace(run_tool=lambda *a: called.set()))
    exec(compile(module, "<actual dispatcher>", "exec"), namespace)

    async def run():
        lock.acquire()
        task = asyncio.create_task(namespace["execute_tool"]("plc_write", {}, "fixture", 12))
        try:
            assert await asyncio.to_thread(queued.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            lock.release()
        assert await asyncio.to_thread(finished.wait, 2)
        assert not called.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"status": "failed"}, {"verified": False}, {"success": False},
    {"failedProjects": 1, "errorCount": 0}, {"diagnosticsPending": True},
    {"errorCount": None}, {"status": "incomplete"},
])
def test_failure_protocol(payload):
    assert not tool_succeeded(payload)


@pytest.mark.parametrize("build", [{}, {"ok": False}, {"failedProjects": 0},
    {"failedProjects": 0, "errorCount": 0, "diagnosticsPending": True},
    {"failedProjects": False, "errorCount": 0}])
def test_verification_fails_closed_on_incomplete_build_diagnostics(build):
    with patch.object(ac, "ps_com", return_value=[{"name": "MAIN"}]), \
         patch.object(ac, "execute_plc_build", return_value=build), \
         patch.object(ac, "analyze_objects", return_value={"summary": {"errors": 0}}):
        result = ac._plc_verify_workflow({})
    assert not result["verified"]
    assert not result["gate"]["build"]


@pytest.mark.parametrize("objects,analysis,expected", [
    ([], {"summary": {"errors": 0}}, False),
    ([{"name": "MAIN"}], {}, False),
    ([{"name": "MAIN"}], {"summary": {"errors": 0}}, True),
])
def test_source_verification_requires_objects_and_static_evidence(objects, analysis, expected):
    with patch.object(ac, "ps_com", return_value=objects), \
         patch.object(ac, "execute_plc_build", return_value={"buildPerformed": True,
             "failedProjects": 0, "errorCount": 0, "errors": [],
             "diagnosticsAvailable": True, "compiler_verified": True}), \
         patch.object(ac, "analyze_objects", return_value=analysis):
        result = ac._plc_verify_workflow({})
    assert result["verified"] is expected


def cache_fixture():
    cache = PlcSourceCache()
    cache.put({"path": "C:/Fixture/A/MAIN.TcPOU", "tree_path": "TIPC^A^POUs^MAIN",
               "xae_pid": 12, "solution": "A.sln", "declaration": "PROGRAM MAIN",
               "implementation": "n := 1;"})
    return cache


def test_cache_partitions_and_explicit_path():
    cache = cache_fixture()
    with cache_scope(12, "A.sln"):
        assert cache.get("MAIN", area="declaration")["declaration"] == "PROGRAM MAIN"
        assert cache.get("MAIN")["implementation"] == "n := 1;"
        assert cache.get("MAIN", path="TIPC^B^POUs^MAIN") is None
        assert cache.get("MAIN", path="TIPC^A^POUs^MAIN") is not None
    for pid, solution in [(13, "A.sln"), (12, "B.sln"), (0, "")]:
        with cache_scope(pid, solution):
            assert cache.get("MAIN") is None


def test_cache_never_relabels_legacy_or_partial_text():
    cache = PlcSourceCache()
    base = {"path": "C:/Fixture/MAIN.TcPOU", "xae_pid": 12, "solution": "A.sln"}
    with cache_scope(12, "A.sln"):
        cache.put({**base, "content": "ambiguous text"})
        assert cache.get("MAIN", area="implementation") is None
        cache.put({**base, "implementation": "n := 1;"})
        assert cache.get("MAIN", area="declaration") is None
        assert cache.get("MAIN") is None
        cache.put({**base, "implementation": "", "declaration": "PROGRAM MAIN"})
        assert cache.get("MAIN")["implementation"] == ""


def test_cache_disk_change_and_event_invalidate_reads(tmp_path):
    file = tmp_path / "MAIN.TcPOU"
    file.write_text("one")
    cache = PlcSourceCache()
    cache.put({"path": str(file), "xae_pid": 12, "solution": "A.sln", "implementation": "one"})
    with cache_scope(12, "A.sln"):
        assert cache.get("MAIN", area="implementation")
        file.write_text("changed")
        assert cache.get("MAIN", area="implementation") is None
    policy = ReadPolicy()
    policy.record("tc_hmi_read", {"file": "Main.view"}, ok=True)
    assert policy.duplicate("tc_hmi_read", {"file": "Main.view"})
    backend.PLC_SOURCE_CACHE.invalidate()
    assert not policy.duplicate("tc_hmi_read", {"file": "Main.view"})


@pytest.mark.parametrize("name", ["plc_read_smart", "plc_read_fast", "plc_search", "plc_source_catalog", "plc_source_index"])
def test_plc_tools_revalidate_sources_instead_of_replaying_whole_turn(name):
    assert not saved_read(name, {})


def test_saved_solution_is_task_scoped_even_when_calls_interleave():
    with patch.object(ac, "ps_com", side_effect=AssertionError("unexpected COM")):
        with cache_scope(12, "C:/A/Machine.sln"):
            first = ac._saved_solution()
            with cache_scope(13, "C:/B/Machine.sln"):
                assert ac._saved_solution() != first
            assert ac._saved_solution() == first


def test_smart_fallback_never_promotes_cached_text_to_authoritative():
    with patch.object(ac, "_saved_solution", return_value="Fixture.sln"), \
         patch.object(ac, "read_indexed_source", side_effect=FileNotFoundError("missing")), \
         patch.object(ac.PLC_SOURCE_CACHE, "get", return_value=None), \
         patch.object(ac, "ps_com", return_value={"declaration": "PROGRAM MAIN", "implementation": "live"}) as live:
        result = ac._plc_read_smart({"name": "MAIN"})
    assert result["source"] == "live_com_fallback"
    assert result["implementation"] == "live"
    assert live.call_args.kwargs["max_lines"] == 0


def test_cache_refresh_checks_host_solution_and_member():
    live = {"declaration": "PROGRAM MAIN", "implementation": "n := 1;",
            "active_document": {"source_file": "C:/Fixture/MAIN.TcPOU", "member": "Run"}}
    with patch.object(ac, "ps_com", return_value={"solution": "A.sln"}), \
         patch.object(ac, "_plc_read_current", return_value=live):
        with pytest.raises(ValueError):
            backend._refresh_plc_cache(12, "B.sln")
        with pytest.raises(ValueError):
            backend._refresh_plc_cache(12, "A.sln", {"path": "C:/Fixture/MAIN.TcPOU", "member": "Stop"})
        with pytest.raises(ValueError):
            backend._refresh_plc_cache(0, "A.sln")


def test_discovery_follows_references_not_neighbour_directories(tmp_path):
    live = tmp_path / "Live" / "PLC.plcproj"
    live.parent.mkdir()
    live.write_text("<Project />")
    backup = tmp_path / ".TwinCATAgent" / "backup" / "Old.plcproj"
    backup.parent.mkdir(parents=True)
    backup.write_text("<Project />")
    removed = tmp_path / "Removed.plcproj"
    removed.write_text("<Project />")
    tsproj = tmp_path / "Machine.tsproj"
    tsproj.write_text('<TcSmProject><Project><Plc><Project PrjFilePath="Live/PLC.plcproj" /></Plc></Project></TcSmProject>')
    sln = tmp_path / "Machine.sln"
    sln.write_text('Project("{type}") = "Machine", "Machine.tsproj", "{id}"\n')
    assert discover_projects(sln) == [live]
    sln.write_text("")
    assert discover_projects(sln) == []
    assert backup not in discover_projects(tmp_path)  # Explicit offline scan.
    tsproj.write_text('<Project PrjFilePath="Missing.plcproj" />')
    with pytest.raises(FileNotFoundError):
        discover_projects(tsproj)


@pytest.mark.parametrize("repo,accepted", [("TwinCAT-Agent", True), ("TwinCAT-Agent-", False), ("Other", False)])
def test_update_feed_and_download_allowlist(repo, accepted):
    manifest = {"version": "9.0.0", "installer": {"url":
        f"https://github.com/AutomationAgent-Code/{repo}/releases/download/v9/Setup.exe", "sha256": "a" * 64}}
    with patch.object(backend.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
        assert backend._update_status()["ok"] is accepted
    root = Path(backend.__file__).parent.parent
    assert "TwinCAT-Agent/releases/latest" in backend.UPDATE_MANIFEST_URL
    assert "'AutomationAgent-Code/TwinCAT-Agent'" in (root / "scripts/Publish-GitHubRelease.ps1").read_text(encoding="utf-8-sig")
    assert "/AutomationAgent-Code/TwinCAT-Agent/releases/download/" in (root / "tc_agent_vsix/TwinCATAgentChatControl.xaml.cs").read_text(encoding="utf-8-sig")
