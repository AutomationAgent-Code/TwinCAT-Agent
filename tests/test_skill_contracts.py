"""Executable documentation contracts; all external mutations are mocked."""
from pathlib import Path
import re
import shlex
from unittest.mock import Mock

from click.testing import CliRunner
import pytest

from tc_template import cli, plc, _ps_bridge


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / ".claude" / "commands"


@pytest.mark.parametrize("filename", ["twincat-agent.md", "twincat-platform.md"])
def test_documented_direct_routes_reach_cli_without_discovery(filename, monkeypatch):
    text = (COMMANDS / filename).read_text(encoding="utf-8")
    samples = re.findall(r"tc target add -n [\w-]+ -a [\d.]+ --auto-auth", text)
    assert samples, "At least one concrete direct-route example must be exercised"
    route = Mock(return_value={"status": "added", "name": "test", "address": "test", "net_id": "test"})
    discovery = Mock(side_effect=AssertionError("Direct route must not scan adapters or network"))
    monkeypatch.setattr(cli, "add_static_route", route)
    monkeypatch.setattr(cli, "list_local_adapters", discovery)
    monkeypatch.setattr(cli, "search_network_targets", discovery)
    for sample in samples:
        args = shlex.split(sample)
        result = CliRunner().invoke(cli.main, args)
        assert result.exit_code == 0, result.output
        name, address = args[4], args[6]
        route.assert_called_with(name, address, address + ".1.1", user="Administrator", password="1")
    discovery.assert_not_called()


def test_documented_subtypes_match_com_creation_map():
    text = (COMMANDS / "plc-programming.md").read_text(encoding="utf-8")
    labels = {"Program": "program", "Function Block": "fb", "Function": "function",
              "Struct (DUT)": "struct", "Enum (DUT)": "enum", "Union (DUT)": "union",
              "GVL": "gvl", "Interface": "interface", "Visualization": "visu"}
    for label, key in labels.items():
        cells = re.findall(r"^\| " + re.escape(label) + r" \| ([*\d]+) \|", text, re.MULTILINE)
        assert len(cells) == 1, f"Missing or duplicated subtype for {label}"
        assert int(cells[0].strip("*")) == plc._POU_TYPES_COM[key][0]


@pytest.mark.parametrize("command,expected", [
    ("remove-project", "com_remove_plc_project"),
    ("delete-project", "com_delete_plc_project"),
])
def test_project_removal_commands_dispatch_to_distinct_operations(command, expected, monkeypatch):
    remove = Mock(return_value={"name": "PLC_Test", "files_deleted": False})
    delete = Mock(return_value={"name": "PLC_Test", "files_deleted": True})
    monkeypatch.setattr(_ps_bridge, "com_remove_plc_project", remove)
    monkeypatch.setattr(_ps_bridge, "com_delete_plc_project", delete)
    result = CliRunner().invoke(cli.main, ["plc", command, "PLC_Test"])
    assert result.exit_code == 0, result.output
    selected, other = (remove, delete) if expected == "com_remove_plc_project" else (delete, remove)
    selected.assert_called_once_with("PLC_Test")
    other.assert_not_called()


@pytest.mark.parametrize("filename", ["twincat-agent.md", "twincat-helper.md", "plc-programming.md"])
def test_skill_local_references_resolve(filename):
    path = COMMANDS / filename
    for target in re.findall(r"\]\(([^)]+\.md)\)", path.read_text(encoding="utf-8")):
        assert (path.parent / target).is_file(), target
