from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_template import _ps_bridge


ROOT = Path(__file__).resolve().parents[1]


class LargeProjectToolTests(unittest.TestCase):
    def test_large_write_payload_uses_temporary_args_file(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            self.assertIn("-ArgsFile", argv)
            self.assertNotIn("-ArgsJson", argv)
            path = Path(argv[argv.index("-ArgsFile") + 1])
            observed["path"] = path
            observed["args"] = json.loads(path.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":"ok"}\n', "")

        code = "x := x + 1;\n" * 1000
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "powershell"}), \
             patch("tc_template._ps_bridge.subprocess.run", side_effect=fake_run):
            result = _ps_bridge.ps_com(
                "write-pou", name="FB_Large", area="implementation", code=code
            )

        self.assertEqual("ok", result)
        self.assertEqual(code, observed["args"]["code"])
        self.assertFalse(observed["path"].exists())

    def test_small_payload_stays_on_command_line(self) -> None:
        def fake_run(argv, **kwargs):
            self.assertIn("-ArgsJson", argv)
            self.assertNotIn("-ArgsFile", argv)
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":{}}\n', "")

        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "powershell"}), \
             patch("tc_template._ps_bridge.subprocess.run", side_effect=fake_run):
            _ps_bridge.ps_com("read-pou", name="MAIN")

    def test_tree_path_payload_uses_args_file_to_preserve_caret(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            self.assertIn("-ArgsFile", argv)
            self.assertNotIn("-ArgsJson", argv)
            path = Path(argv[argv.index("-ArgsFile") + 1])
            observed["args"] = json.loads(path.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":{}}\n', "")

        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "powershell"}), \
             patch("tc_template._ps_bridge.subprocess.run", side_effect=fake_run):
            _ps_bridge.ps_com("system-remove", path="TIRT^PlcTask1", apply=False)

        self.assertEqual("TIRT^PlcTask1", observed["args"]["path"])

    def test_slash_tree_path_is_normalized_for_shell_safe_cli_use(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            self.assertIn("-ArgsFile", argv)
            path = Path(argv[argv.index("-ArgsFile") + 1])
            observed["args"] = json.loads(path.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":{}}\n', "")

        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "powershell"}), \
             patch("tc_template._ps_bridge.subprocess.run", side_effect=fake_run):
            _ps_bridge.ps_com("system-remove", path="TIRT/PlcTask1", apply=False)

        self.assertEqual("TIRT^PlcTask1", observed["args"]["path"])

    def test_com_search_no_longer_dumps_all_code_to_python(self) -> None:
        source = (ROOT / "tc_template" / "_ps_bridge.py").read_text(encoding="utf-8-sig")
        search_body = source.split("def search_code_result(", 1)[1].split(
            "# ---- PLC project CRUD", 1
        )[0]
        self.assertIn('"search-code"', search_body)
        self.assertNotIn("com_all_code()", search_body)

    def test_code_inventory_uses_temp_file_for_source_payload(self) -> None:
        observed = {}

        def fake_ps_com(command, **kwargs):
            self.assertEqual("code-inventory", command)
            output = Path(kwargs["output_file"])
            observed["path"] = output
            output.write_text('{"solution":"C:/Machine.sln","objects":[]}', encoding="utf-8")
            return {"written_to": str(output)}

        with patch("tc_template._ps_bridge.ps_com", side_effect=fake_ps_com):
            result = _ps_bridge.com_code_inventory(include_code=True)
        self.assertEqual("C:/Machine.sln", result["solution"])
        self.assertFalse(observed["path"].exists())

    def test_native_bridge_uses_direct_lookup_and_bounded_search(self) -> None:
        source = (ROOT / "tc_template" / "_native_bridge.py").read_text(encoding="utf-8")
        self.assertIn("sysman.LookupTreeItem(path)", source)
        self.assertIn("def _find_pou", source)
        self.assertIn("def _search_code", source)
        self.assertIn("def _patch_pou", source)
        self.assertIn("max_results", source)
        self.assertIn("max_lines", source)
        self.assertIn('item_type == 601', source)

    def test_powershell_bridge_scans_custom_folders_by_item_type(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("function Get-PlcObjectCategory", source)
        self.assertIn("function Get-PlcObjectFrames", source)
        self.assertIn("Get-PlcObjectFrames $Dte -Folders $Folders", source)
        self.assertIn("Get-PlcObjectCategory ([int]$o.ItemType)", source)

    def test_exact_tree_path_flows_through_python_bridge(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_ps_com(command, **kwargs):
            calls.append((command, kwargs))
            return {}

        path = "TIPC^PLC^PLC Project^POUs^FBs^FB_Door"
        with patch("tc_template._ps_bridge.ps_com", side_effect=fake_ps_com):
            _ps_bridge.com_read_pou("FB_Door", path=path)
            _ps_bridge.com_write_pou("FB_Door", "", path=path)
            _ps_bridge.com_patch_pou("FB_Door", "old", "new", path=path)
            _ps_bridge.search_code_result("door", pou="FB_Door", path=path)

        # 写入与局部替换会先读取当前对象进行写入前审查，随后才发出 mutation。
        self.assertEqual(6, len(calls))
        self.assertTrue(all(kwargs["path"] == path for _, kwargs in calls))

    def test_agent_tools_expose_exact_tree_path(self) -> None:
        source = (ROOT / "tc_agent" / "agent_core.py").read_text(encoding="utf-8")
        for tool in ("plc_read", "plc_search", "plc_write", "plc_patch", "plc_create_member"):
            body = source.split(f'_tool("{tool}"', 1)[1].split("_tool(", 1)[0]
            self.assertIn('"path"', body, tool)


if __name__ == "__main__":
    unittest.main()
