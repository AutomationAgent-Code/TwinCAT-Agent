from __future__ import annotations

from pathlib import Path

import pytest

from tc_agent.plc_source_index import catalog, read, search, status


def _make_project(root: Path) -> tuple[Path, Path]:
    solution = root / "Machine.sln"
    solution.write_text('Project("{fixture}") = "PLC", "PLC/PLC.plcproj", "{id}"\n', encoding="utf-8")
    project = root / "PLC" / "PLC.plcproj"
    source = project.parent / "POUs" / "MAIN.TcPOU"
    source.parent.mkdir(parents=True)
    project.write_text("""<Project><PropertyGroup><Name>PLC</Name></PropertyGroup>
<ItemGroup><Compile Include="POUs\\MAIN.TcPOU" /></ItemGroup></Project>""", encoding="utf-8")
    source.write_text("""<TcPlcObject><POU Name="MAIN"><Declaration>PROGRAM MAIN</Declaration>
<Implementation><ST>nValue := 1;</ST></Implementation>
<Method Name="Start"><Declaration>METHOD Start</Declaration><Implementation><ST>bRun := TRUE;</ST></Implementation></Method>
<Property Name="Value"><Get><Declaration>GET</Declaration><Implementation><ST>Value := nValue;</ST></Implementation></Get></Property>
</POU></TcPlcObject>""", encoding="utf-8")
    return solution, source


def test_sqlite_index_reads_and_updates_only_changed_file(tmp_path: Path):
    solution, source = _make_project(tmp_path)
    first = status(solution)
    assert first["added"] == 1
    assert first["count"] == 1
    assert first["member_count"] == 3
    result = read(solution, "MAIN")
    assert result["source"] == "disk_index"
    assert result["path_kind"] == "saved_source"
    assert result["path"] == result["source_path"]
    assert result["tree_path_available"] is False
    assert result["implementation"] == "nValue := 1;"
    assert result["index_sync"]["unchanged"] == 1
    method = read(solution, "MAIN", member="Start")
    getter = read(solution, "MAIN", member="Value.Get")
    assert method["implementation"] == "bRun := TRUE;"
    assert getter["implementation"] == "Value := nValue;"

    source.write_text("""<TcPlcObject><POU Name="MAIN"><Declaration>PROGRAM MAIN</Declaration>
<Implementation><ST>nValue := 2;</ST></Implementation></POU></TcPlcObject>""", encoding="utf-8")
    updated = read(solution, "MAIN")
    assert updated["implementation"] == "nValue := 2;"
    assert updated["index_sync"]["updated"] == 1


def test_sqlite_index_removes_deleted_sources(tmp_path: Path):
    solution, source = _make_project(tmp_path)
    status(solution)
    source.unlink()
    # Removing from the project file models a source object removed in XAE.
    project = next(tmp_path.rglob("*.plcproj"))
    project.write_text("<Project><PropertyGroup><Name>PLC</Name></PropertyGroup></Project>", encoding="utf-8")
    result = status(solution)
    assert result["removed"] == 1
    assert result["count"] == 0


def test_sqlite_index_searches_roots_and_members(tmp_path: Path):
    solution, _ = _make_project(tmp_path)
    result = search(solution, "nValue")
    assert result["source"] == "disk_index"
    assert result["count"] >= 2
    assert any(item["pou"] == "MAIN.Value.Get" for item in result["matches"])
    regex = search(solution, r"bRun\s*:=", regex=True, pou="MAIN")
    assert regex["count"] == 1
    assert regex["matches"][0]["pou"] == "MAIN.Start"


def test_sqlite_index_catalog_returns_paths_and_member_names_without_source(tmp_path: Path):
    solution, _ = _make_project(tmp_path)
    result = catalog(solution, query="Value")
    assert result["source"] == "disk_index"
    assert result["count"] == 1
    assert result["items"][0]["name"] == "MAIN"
    assert result["items"][0]["path"].endswith("MAIN.TcPOU")
    assert result["items"][0]["path_kind"] == "saved_source"
    assert result["items"][0]["source_path"] == result["items"][0]["path"]
    assert "Value.Get" in result["items"][0]["members"]


def test_sqlite_index_rejects_com_tree_path_instead_of_returning_saved_object(tmp_path: Path):
    solution, _ = _make_project(tmp_path)
    with pytest.raises(ValueError, match="not a saved-source index path"):
        read(solution, "MAIN", path="TIPC^PLC^PLC Project^POUs^MAIN")
