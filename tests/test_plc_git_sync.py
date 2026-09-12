from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch

from tc_agent import agent_core
from tc_agent.plc_git_sync import (
    GitSource, GitSyncError,
    discover_sources,
    parse_source_file,
    prepare_repository,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _pou(path: Path, name: str = "PRG_Main", implementation: str = "nCount := 1;") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TcPlcObject Version="1.1.0.1">\n'
        f'  <POU Name="{name}" Id="{{00000000-0000-0000-0000-000000000001}}">\n'
        f'    <Declaration><![CDATA[PROGRAM {name}\nVAR\n    nCount : INT;\nEND_VAR]]></Declaration>\n'
        f'    <Implementation><ST><![CDATA[{implementation}]]></ST></Implementation>\n'
        '    <Method Name="Reset"><Declaration><![CDATA[METHOD Reset]]></Declaration>'
        '<Implementation><ST><![CDATA[nCount := 0;]]></ST></Implementation></Method>\n'
        '  </POU>\n</TcPlcObject>\n', encoding="utf-8",
    )


def test_parse_native_twincat_source_and_members(tmp_path: Path) -> None:
    source_file = tmp_path / "PLC" / "POUs" / "PRG_Main.TcPOU"
    _pou(source_file)

    source = parse_source_file(source_file, repo_root=tmp_path)

    assert source.name == "PRG_Main"
    assert source.create_type == "program"
    assert source.declaration.startswith("PROGRAM PRG_Main")
    assert source.implementation == "nCount := 1;"
    assert [member.path for member in source.members] == ["Reset"]
    assert source.members[0].kind == "method"


def test_discover_rejects_duplicate_object_names(tmp_path: Path) -> None:
    _pou(tmp_path / "A" / "One.TcPOU")
    _pou(tmp_path / "B" / "Two.TcPOU")

    sources, errors = discover_sources(tmp_path)

    assert len(sources) == 2
    assert any("对象名重复" in item["error"] for item in errors)


def test_remote_preview_does_not_clone_or_write(tmp_path: Path) -> None:
    destination = tmp_path / "checkout"

    result = prepare_repository(
        "https://github.com/example/plc.git",
        local_path=str(destination), apply=False,
    )

    assert result["status"] == "preview_requires_clone"
    assert not destination.exists()


def test_local_repository_pull_requires_clean_tree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "TwinCAT Agent Test")
    _pou(tmp_path / "PLC" / "POUs" / "PRG_Main.TcPOU")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    (tmp_path / "uncommitted.TcPOU").write_text("not committed", encoding="utf-8")

    with pytest.raises(GitSyncError, match="未提交修改"):
        prepare_repository(str(tmp_path), apply=True)


def test_agent_preview_plans_live_xae_update_without_mutation(tmp_path: Path) -> None:
    source = GitSource(
        file=tmp_path / "MAIN.TcPOU", relative_file="MAIN.TcPOU", name="MAIN",
        kind="pou", create_type="program", declaration="PROGRAM MAIN",
        implementation="nCount := 2;", members=(),
    )
    target = {"name": "MAIN", "path": "TIPC^PLC^PLC1^POUs^MAIN", "itemType": 602}
    current = {
        **target, "declaration": "PROGRAM MAIN", "implementation": "nCount := 1;",
    }
    with patch("tc_agent.plc_git_sync.prepare_repository", return_value={
        "status": "checked_out", "local_path": str(tmp_path), "after": "abc",
    }), patch("tc_agent.plc_git_sync.discover_sources", return_value=([source], [])), \
            patch.object(agent_core, "ps_com", side_effect=[
                {"solution": r"C:\Machine\Machine.sln"}, [target], current,
            ]):
        result = agent_core._plc_git_sync({
            "repository": str(tmp_path), "apply": False,
        })

    assert result["status"] == "preview"
    assert result["plan"][0]["changes"] == ["implementation"]
    assert result["xae_reopen_performed"] is False
    assert result["xae_close_performed"] is False


def test_agent_sync_refuses_visible_unsaved_editor(tmp_path: Path) -> None:
    source = GitSource(
        file=tmp_path / "MAIN.TcPOU", relative_file="MAIN.TcPOU", name="MAIN",
        kind="pou", create_type="program", declaration="PROGRAM MAIN",
        implementation="nCount := 2;", members=(),
    )
    target = {"name": "MAIN", "path": "TIPC^PLC^PLC1^POUs^MAIN", "itemType": 602}
    current = {**target, "declaration": "PROGRAM MAIN", "implementation": "nCount := 1;"}
    with patch("tc_agent.plc_git_sync.prepare_repository", return_value={
        "status": "checked_out", "local_path": str(tmp_path), "after": "abc",
    }), patch("tc_agent.plc_git_sync.discover_sources", return_value=([source], [])), \
            patch.object(agent_core, "ps_com", side_effect=[
                {"solution": r"C:\Machine\Machine.sln"}, [target], current,
                {"active_document": {"full_name": r"C:\Machine\MAIN.TcPOU", "saved": False}},
            ]):
        result = agent_core._plc_git_sync({
            "repository": str(tmp_path), "apply": True,
        })

    assert result["status"] == "conflict"
    assert result["written"] is False
    assert result["not_executed"] is True
