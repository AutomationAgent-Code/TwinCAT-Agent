from __future__ import annotations

import os
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tc_template import _com, _native_bridge, _ps_bridge, io_native, plc, tc_platform


ROOT = Path(__file__).resolve().parents[1]


class NativeComBridgeTests(unittest.TestCase):
    def test_find_object_prefers_exact_case_over_visualization_name_collision(self) -> None:
        main = SimpleNamespace()
        visu = SimpleNamespace()
        entries = [
            {"item": main, "name": "MAIN", "path": "TIPC^PLC^POUs^MAIN", "itemType": 602},
            {"item": visu, "name": "Main", "path": "TIPC^PLC^VISUs^Main", "itemType": 619},
        ]
        with patch.object(_native_bridge, "_iter_objects", return_value=entries):
            item, path = _native_bridge._find_object(object(), "MAIN")
        self.assertIs(main, item)
        self.assertEqual("TIPC^PLC^POUs^MAIN", path)

    def test_read_current_reports_when_no_document_is_active(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有活动文档"):
            _native_bridge._read_current(SimpleNamespace(ActiveDocument=None), {})

    def test_read_member_accepts_wrong_type_hint_and_reports_actual_type(self) -> None:
        parent = SimpleNamespace(Name="FB_Test", ItemType=604)
        action = SimpleNamespace(Name="Run", ItemType=608)
        with patch.object(_native_bridge, "_children", side_effect=lambda node: [action] if node is parent else []), \
             patch.object(_native_bridge, "_system_manager", return_value=object()), \
             patch.object(_native_bridge, "_find_object", return_value=(parent, "TIPC^PLC^POUs^FB_Test")):
            result = _native_bridge._read_pou(object(), {
                "name": "FB_Test", "method": "Run", "member_type": "method",
                "area": "implementation",
            })
        self.assertEqual("action", result["member_type"])
        self.assertEqual("method", result["member_type_resolved"]["requested"])
        self.assertEqual("action", result["member_type_resolved"]["actual"])

    def test_native_batch_reads_multiple_slices_in_one_connected_dispatch(self) -> None:
        payloads = [
            {"name": "MAIN", "declaration": "PROGRAM MAIN", "implementation": "x := 1;"},
            {"name": "FB_A", "declaration": "FUNCTION_BLOCK FB_A", "implementation": ""},
        ]
        with patch.object(_native_bridge, "_read_pou", side_effect=payloads) as read:
            result = _native_bridge._read_batch(SimpleNamespace(), {
                "requests": [{"id": "a", "name": "MAIN"},
                             {"id": "b", "name": "FB_A"}],
                "max_total_chars": 50000,
            })
        self.assertEqual(2, read.call_count)
        self.assertEqual(2, result["count"])
        self.assertTrue(result["live_xae"])
        self.assertEqual(["a", "b"], [item["request_id"] for item in result["results"]])
        self.assertIn("implementation", result["results"][0]["hashes"])

    def test_read_tool_does_not_change_xae_silent_mode(self) -> None:
        class Dte:
            get_object_calls = 0

            def GetObject(self, _name):
                self.get_object_calls += 1
                raise AssertionError("read-only tools must not touch SilentMode")

        dte = Dte()
        with patch.object(_native_bridge, "com_apartment", return_value=nullcontext()), \
             patch.object(_native_bridge, "target_process", return_value=nullcontext()), \
             patch.object(_native_bridge, "get_active_dte", return_value=dte), \
             patch.object(_native_bridge, "_dispatch_connected", return_value={"ok": True}):
            result = _native_bridge.dispatch("read-pou", {"name": "MAIN"})
        self.assertEqual({"ok": True}, result)
        self.assertEqual(0, dte.get_object_calls)

    def test_runtime_tool_restores_silent_mode_after_failure(self) -> None:
        class Settings:
            def __init__(self):
                self._value = False
                self.writes = []

            @property
            def SilentMode(self):
                return self._value

            @SilentMode.setter
            def SilentMode(self, value):
                self._value = bool(value)
                self.writes.append(bool(value))

        settings = Settings()
        dte = SimpleNamespace(GetObject=lambda _name: settings)

        def fail_while_silent(*_args):
            self.assertTrue(settings.SilentMode)
            raise RuntimeError("runtime failed")

        with patch.object(_native_bridge, "com_apartment", return_value=nullcontext()), \
             patch.object(_native_bridge, "target_process", return_value=nullcontext()), \
             patch.object(_native_bridge, "get_active_dte", return_value=dte), \
             patch.object(_native_bridge, "_dispatch_connected", side_effect=fail_while_silent):
            with self.assertRaisesRegex(RuntimeError, "runtime failed"):
                _native_bridge.dispatch("activate")
        self.assertFalse(settings.SilentMode)
        self.assertEqual([True, False], settings.writes)

    def test_automated_flow_restores_silent_and_suppress_ui(self) -> None:
        settings = SimpleNamespace(SilentMode=False)

        class Dte:
            SuppressUI = False

            def GetObject(self, _name):
                return settings

        dte = Dte()

        @tc_platform._preserve_xae_ui_state
        def automated_flow():
            settings.SilentMode = True
            dte.SuppressUI = True
            return "done"

        with patch.object(tc_platform, "_dte", return_value=dte):
            self.assertEqual("done", automated_flow())
        self.assertFalse(settings.SilentMode)
        self.assertFalse(dte.SuppressUI)

    def test_automated_flow_restores_ui_state_after_exception(self) -> None:
        settings = SimpleNamespace(SilentMode=False)

        class Dte:
            SuppressUI = False

            def GetObject(self, _name):
                return settings

        dte = Dte()

        @tc_platform._preserve_xae_ui_state
        def automated_flow():
            settings.SilentMode = True
            dte.SuppressUI = True
            raise RuntimeError("flow failed")

        with patch.object(tc_platform, "_dte", return_value=dte):
            with self.assertRaisesRegex(RuntimeError, "flow failed"):
                automated_flow()
        self.assertFalse(settings.SilentMode)
        self.assertFalse(dte.SuppressUI)

    def test_powershell_dispatch_scopes_silent_mode(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(encoding="utf-8-sig")
        dispatcher = source[source.index("if ($Command) {"):]
        self.assertNotIn("Set-TcSilentMode $dte $true", dispatcher)
        self.assertIn("$previousSilentMode = [bool]$silentSettings.SilentMode", dispatcher)
        self.assertIn("$silentSettings.SilentMode = $previousSilentMode", dispatcher)

    def test_new_folder_uses_exact_parent_tree_path(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=()):
                self.Name = name
                self.ItemType = item_type
                self.children = list(children)
                self.created = []

            def __iter__(self):
                return iter(self.children)

            def CreateChild(self, name, item_type, _unused, _info):
                child = Node(name, item_type)
                self.children.append(child)
                self.created.append(child)
                return child

        parent = Node("00_Local", 601)
        class SysManager:
            def LookupTreeItem(self, _path):
                return parent

        sysman = SysManager()
        with (
            patch("tc_template._native_bridge._system_manager", return_value=sysman),
            patch("tc_template._native_bridge._plc_roots", return_value=iter((
                ("TIPC^PLC1^PLC1 Project", "PLC1 Project", object()),
            ))),
        ):
            result = _native_bridge._new_folder(object(), {
                "name": "02_Auto",
                "parent_path": "TIPC^PLC1^PLC1 Project^POUs^00_Local",
            })
        self.assertEqual("created", result["status"])
        self.assertEqual(
            "TIPC^PLC1^PLC1 Project^POUs^00_Local^02_Auto", result["path"])
        self.assertEqual(["02_Auto"], [child.Name for child in parent.children])

    def test_find_member_supports_action_item_type_and_type_hint(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=()):
                self.Name = name
                self.ItemType = item_type
                self.children = list(children)

            def __iter__(self):
                return iter(self.children)

        action = Node("A_01_Auto", 608)
        parent = Node("FB_Machine", 604, [action])
        self.assertIs(action, _native_bridge._find_member(
            parent, "A_01_Auto", "action"))
        self.assertIs(action, _native_bridge._find_member(
            parent, "A_01_Auto", "method"))

    def test_delete_member_removes_exact_method_from_parent(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=()):
                self.Name = name
                self.ItemType = item_type
                self.children = list(children)

            def __iter__(self):
                return iter(self.children)

            def DeleteChild(self, name):
                self.children = [item for item in self.children if item.Name != name]

        method = Node("Start", 609)
        parent = Node("FB_Motor", 604, [method, Node("Stop", 609)])
        with patch("tc_template._native_bridge._system_manager", return_value=object()), \
             patch("tc_template._native_bridge._find_object",
                   return_value=(parent, "TIPC^PLC^POUs^FB_Motor")), \
             patch("tc_template._native_bridge._symbol_references", return_value=[]):
            result = _native_bridge._delete_member(object(), {
                "pou": "FB_Motor", "name": "Start", "type": "method",
            })
        self.assertEqual("deleted", result["status"])
        self.assertEqual(["Stop"], [item.Name for item in parent.children])

    def test_delete_member_removes_property_accessor(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=()):
                self.Name = name
                self.ItemType = item_type
                self.children = list(children)

            def __iter__(self):
                return iter(self.children)

            def DeleteChild(self, name):
                self.children = [item for item in self.children if item.Name != name]

        prop = Node("Position", 611, [Node("Get", 613), Node("Set", 614)])
        parent = Node("FB_Axis", 604, [prop])
        with patch("tc_template._native_bridge._system_manager", return_value=object()), \
             patch("tc_template._native_bridge._find_object",
                   return_value=(parent, "TIPC^PLC^POUs^FB_Axis")), \
             patch("tc_template._native_bridge._symbol_references", return_value=[]):
            result = _native_bridge._delete_member(object(), {
                "pou": "FB_Axis", "name": "Position", "type": "propget",
            })
        self.assertEqual("Position.Get", result["member"])
        self.assertEqual(["Set"], [item.Name for item in prop.children])

    def test_delete_object_preview_reports_reference_gate(self) -> None:
        item = SimpleNamespace(Name="FB_Test", ItemType=604)
        with patch("tc_template._native_bridge._system_manager", return_value=object()), \
             patch("tc_template._native_bridge._find_object",
                   return_value=(item, "TIPC^PLC^Project^POUs^FB_Test")), \
             patch("tc_template._native_bridge._symbol_references",
                   return_value=[{"pou": "MAIN", "line": 4}]):
            result = _native_bridge._delete_object(object(), {
                "name": "FB_Test", "dry_run": True,
            })
        self.assertEqual("preview", result["status"])
        self.assertTrue(result["would_block"])
        self.assertEqual(1, result["reference_count"])

    def test_rename_member_uses_exact_parent_path(self) -> None:
        class Node:
            def __init__(self, name, children=()):
                self.Name = name
                self.children = list(children)

            def __iter__(self):
                return iter(self.children)

        member = Node("Start")
        parent = Node("FB_Test", [member])
        exact = "TIPC^PLC^Project^POUs^FB_Test"
        with patch("tc_template._native_bridge._system_manager", return_value=object()), \
             patch("tc_template._native_bridge._find_object",
                   return_value=(parent, exact)) as find:
            result = _native_bridge._rename_member(object(), {
                "pou": "FB_Test", "old": "Start", "new": "Run", "path": exact,
            })
        find.assert_called_once_with(unittest.mock.ANY, "FB_Test", exact)
        self.assertEqual("Run", member.Name)
        self.assertEqual("renamed", result["status"])

    def test_interface_property_member_payload_keeps_accessor_names_without_text(self) -> None:
        class Node:
            def __init__(self, name, item_type=0, children=(), **attrs):
                self.Name = name
                self.ItemType = item_type
                self._children = list(children)
                for key, value in attrs.items():
                    setattr(self, key, value)

            def __iter__(self):
                return iter(self._children)

        getter = Node("Get")
        setter = Node("Set")
        property_node = Node("Prop", 612, (getter, setter),
                             DeclarationText="PROPERTY Prop : BOOL")

        payload = _native_bridge._member_payload(property_node, include_code=True)

        self.assertEqual("Prop", payload["name"])
        self.assertEqual(612, payload["itemType"])
        self.assertEqual("PROPERTY Prop : BOOL", payload["declaration"])
        self.assertEqual(["Get", "Set"], [item["name"] for item in payload["members"]])
        self.assertNotIn("declaration", payload["members"][0])

    def test_native_worker_forces_utf8_for_com_json_transport(self) -> None:
        source = (ROOT / "tc_template" / "_native_worker.py").read_text(encoding="utf-8")
        self.assertIn('stream.reconfigure(encoding="utf-8", errors="strict")', source)
        self.assertIn("_force_utf8_stdio()", source)

    def test_connect_check_accepts_dte_without_main_window(self) -> None:
        class DteWithoutMainWindow:
            Name = "TcXaeShell"
            Solution = SimpleNamespace(FullName=r"D:\\Demo\\Demo.sln")

            def __getattr__(self, name):
                if name == "MainWindow":
                    raise AttributeError("unknown.MainWindow")
                raise AttributeError(name)

        result = _native_bridge._connect_check(DteWithoutMainWindow(), 4024)
        self.assertEqual(r"D:\\Demo\\Demo.sln", result["solution"])
        self.assertEqual(4024, result["pid"])
        self.assertEqual("native-python-com", result["bridge"])

    def test_custom_plc_folders_are_scanned_by_item_type(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=(), **attrs):
                self.Name = name
                self.ItemType = item_type
                self._children = list(children)
                for key, value in attrs.items():
                    setattr(self, key, value)

            def __iter__(self):
                return iter(self._children)

        fb = Node("FB_GMS", 604, DeclarationText="FUNCTION_BLOCK FB_GMS")
        dut = Node("ST_GMS", 606, DeclarationText="TYPE ST_GMS : STRUCT")
        alias = Node("ST_ProcessAlias", 623, DeclarationText="TYPE ST_ProcessAlias : INT")
        gvl = Node("GVL_GMS", 615, DeclarationText="VAR_GLOBAL")
        nested_folder = Node("05GMS90", 601, (fb, dut, alias))
        custom_folder = Node("00Function功能块应用", 601, (nested_folder, gvl))
        task = Node("Task_10ms", 621)
        project = Node("Test项目", 600, (custom_folder, task))
        controller = Node("Test", 56, (project,), NestedProject=project)
        tipc = Node("PLC", 14, (controller,))

        class SysManager:
            def LookupTreeItem(self, path):
                if path == "TIPC":
                    return tipc
                raise KeyError(path)

        sysman = SysManager()
        entries = list(_native_bridge._iter_objects(sysman))
        self.assertEqual(["FB_GMS", "ST_GMS", "ST_ProcessAlias", "GVL_GMS"],
                         [entry["name"] for entry in entries])
        self.assertEqual(["POUs", "DUTs", "DUTs", "GVLs"],
                         [entry["folder"] for entry in entries])
        self.assertEqual(2, entries[0]["depth"])
        self.assertIn("00Function功能块应用^05GMS90^FB_GMS",
                      entries[0]["path"])

        found, path = _native_bridge._find_object(sysman, "FB_GMS")
        self.assertIs(fb, found)
        self.assertEqual(entries[0]["path"], path)

        with patch("tc_template._native_bridge._system_manager", return_value=sysman):
            structure = _native_bridge._structure(SimpleNamespace(), {
                "limit_per_folder": 0,
                "include_members": False,
            })
        self.assertEqual(1, structure["counts"]["POUs"])
        self.assertEqual(2, structure["counts"]["DUTs"])
        self.assertEqual(1, structure["counts"]["GVLs"])
        self.assertEqual("FB_GMS", structure["folders"]["POUs"][0]["name"])

    def test_object_lookup_searches_all_plc_projects_and_requires_path_for_duplicates(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=(), **attrs):
                self.Name = name
                self.ItemType = item_type
                self._children = list(children)
                for key, value in attrs.items():
                    setattr(self, key, value)

            def __iter__(self):
                return iter(self._children)

        primary = Node("Primary Project", 600, (Node("ST_Primary", 606),))
        secondary_value = Node("ST_ProcParam", 606)
        secondary = Node("Process Project", 600, (secondary_value,))
        ctrl_one = Node("PLC_Main", 56, (primary,), NestedProject=primary)
        ctrl_two = Node("PLC_Process", 56, (secondary,), NestedProject=secondary)
        tipc = Node("TIPC", 14, (ctrl_one, ctrl_two))

        class SysManager:
            def LookupTreeItem(self, path):
                if path == "TIPC":
                    return tipc
                raise KeyError(path)

        sysman = SysManager()
        found, path = _native_bridge._find_object(sysman, "ST_ProcParam")
        self.assertIs(secondary_value, found)
        self.assertEqual("TIPC^PLC_Process^Process Project^ST_ProcParam", path)

        duplicate = Node("ST_ProcParam", 606)
        primary._children.append(duplicate)
        with self.assertRaisesRegex(ValueError, "multiple PLC projects"):
            _native_bridge._find_object(sysman, "ST_ProcParam")

    def test_com_busy_retry_recovers_after_xae_accepts_calls(self) -> None:
        attempts = 0

        def flaky_call():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise Exception(-2147418111, "Call was rejected by callee")
            return "ok"

        with patch("tc_template._com.time.sleep") as sleep:
            result = _com.retry_com_busy(flaky_call, initial_delay=0.01)
        self.assertEqual("ok", result)
        self.assertEqual(3, attempts)
        self.assertEqual(2, sleep.call_count)

    def test_com_busy_retry_does_not_hide_real_com_errors(self) -> None:
        with self.assertRaisesRegex(Exception, "permanent failure"), \
             patch("tc_template._com.time.sleep") as sleep:
            _com.retry_com_busy(lambda: (_ for _ in ()).throw(
                Exception(-2147024891, "permanent failure")
            ))
        sleep.assert_not_called()

    def test_error_item_levels_follow_envdte80_values(self) -> None:
        error = _native_bridge._error_item_payload(SimpleNamespace(
            ErrorLevel=4, ErrorCode="C0004", Description="bad member",
            Project="PLC1", FileName="MAIN.TcPOU", Line=12,
        ))
        warning = _native_bridge._error_item_payload(SimpleNamespace(
            ErrorLevel=2, ErrorCode="", Description="warning",
            Project="PLC1", FileName="MAIN.TcPOU", Line=3,
        ))
        self.assertEqual("error", error["severity"])
        self.assertEqual("warning", warning["severity"])

    def test_parse_chinese_error_list_tsv(self) -> None:
        copied = (
            "严重性\t代码\t说明\t项目\t文件\t行\n"
            "错误\t\tC0004: 成员不存在\tPLC1\tMAIN.TcPOU\t35\n"
            "警告\tC0195\t未使用的变量\tPLC1\tMAIN.TcPOU\t8\n"
        )
        items = _native_bridge._parse_error_list_tsv(copied)
        self.assertEqual(2, len(items))
        self.assertEqual("error", items[0]["severity"])
        self.assertEqual("C0004", items[0]["code"])
        self.assertEqual(35, items[0]["line"])
        self.assertEqual("warning", items[1]["severity"])

    def test_build_uses_only_non_invasive_error_items(self) -> None:
        class SolutionBuild:
            LastBuildInfo = 1

            def Build(self, wait_for_build):
                self.wait_for_build = wait_for_build

        build = SolutionBuild()
        dte = SimpleNamespace(Solution=SimpleNamespace(SolutionBuild=build))
        items = [
            {"severity": "error", "code": "C0004", "description": "bad",
             "project": "PLC1", "file": "MAIN.TcPOU", "line": 2},
            {"severity": "warning", "code": "C0195", "description": "unused",
             "project": "PLC1", "file": "MAIN.TcPOU", "line": 3},
        ]
        with patch("tc_template._native_bridge._read_error_items_com",
                   return_value=items), \
             patch("tc_template._native_bridge._read_error_items_ui") as ui_copy:
            result = _native_bridge._build(dte, {})
        self.assertTrue(build.wait_for_build)
        self.assertEqual(1, result["failedProjects"])
        self.assertEqual(2, result["errorCount"])
        self.assertEqual("dte-error-items", result["errorSource"])
        self.assertEqual({"C0004", "C0195"}, {
            item["code"] for item in result["errors"]
        })
        ui_copy.assert_not_called()

    def test_build_retries_error_items_and_promotes_medium_plc_diagnostics(self) -> None:
        class SolutionBuild:
            LastBuildInfo = 1

            def Build(self, wait_for_build):
                self.wait_for_build = wait_for_build

        build = SolutionBuild()
        dte = SimpleNamespace(Solution=SimpleNamespace(SolutionBuild=build))
        delayed = [{"severity": "warning", "code": "C0004",
                    "description": "C0004: member does not exist",
                    "project": "PLC1", "file": "MAIN.TcPOU", "line": 12}]
        with patch("tc_template._native_bridge._read_error_items_com",
                   side_effect=[[], [], delayed]) as read, \
             patch("tc_template._native_bridge.time.sleep") as sleep:
            result = _native_bridge._build(dte, {})
        self.assertEqual(3, read.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(1, result["errorCount"])
        self.assertEqual("error", result["errors"][0]["severity"])
        self.assertFalse(result["diagnosticsPending"])

    def test_build_marks_diagnostics_pending_when_error_list_stays_empty(self) -> None:
        class SolutionBuild:
            LastBuildInfo = 1

            def Build(self, wait_for_build):
                pass

        dte = SimpleNamespace(Solution=SimpleNamespace(SolutionBuild=SolutionBuild()))
        with patch("tc_template._native_bridge._read_error_items_com", return_value=[]), \
             patch("tc_template._native_bridge.time.sleep"):
            result = _native_bridge._build(dte, {})
        self.assertTrue(result["diagnosticsPending"])
        self.assertEqual(5, result["errorReadAttempts"])
        self.assertIn("retry", result["message"].lower())

    def test_native_is_default_and_never_starts_powershell(self) -> None:
        with patch.dict(os.environ, {}, clear=False), \
             patch("tc_template._native_bridge.available", return_value=(True, "")), \
             patch("tc_template._native_bridge.dispatch", return_value={"bridge": "native"}) as dispatch, \
             patch("tc_template._ps_bridge.subprocess.run") as run:
            os.environ.pop("TC_AGENT_COM_BACKEND", None)
            result = _ps_bridge.ps_com("connect-check", preferPid=4024)
        self.assertEqual({"bridge": "native"}, result)
        dispatch.assert_called_once_with("connect-check", {"preferPid": 4024})
        run.assert_not_called()

    def test_tool_target_pid_flows_to_native_dispatch(self) -> None:
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "native"}), \
             patch("tc_template._native_bridge.available", return_value=(True, "")), \
             patch("tc_template._native_bridge.dispatch", return_value={}) as dispatch:
            with _ps_bridge.tool_target(12345):
                _ps_bridge.ps_com("state")
        args = dispatch.call_args.args[1]
        self.assertEqual(12345, args["preferPid"])
        self.assertTrue(args["strictPid"])

    def test_program_subtype_matches_twincat_spec(self) -> None:
        self.assertEqual(602, plc._POU_TYPES_COM["program"][0])

    def test_function_subtype_matches_twincat_spec(self) -> None:
        self.assertEqual(603, plc._POU_TYPES_COM["function"][0])

    def test_twincat_text_removes_emoji_and_broken_surrogates(self) -> None:
        source = "// 中文保留 💯\nvalue := 1;\udcaf\x00"
        cleaned, replaced = plc.normalize_twincat_text(source)
        self.assertEqual("// 中文保留  \nvalue := 1;  ", cleaned)
        self.assertEqual(3, replaced)
        self.assertNotIn("💯", cleaned)
        self.assertTrue(all(not 0xD800 <= ord(char) <= 0xDFFF for char in cleaned))
        json.dumps(cleaned, ensure_ascii=False).encode("utf-8")

    def test_native_write_sanitizes_before_com_assignment(self) -> None:
        class Item:
            ImplementationText = ""

        item = Item()
        cleaned, replaced = __import__(
            "tc_template._native_bridge", fromlist=["_write_text"]
        )._write_text(item, "implementation", "// 💯\udcaf\nrun();")
        self.assertEqual(2, replaced)
        self.assertEqual(cleaned, item.ImplementationText)
        self.assertEqual("//   \nrun();", cleaned)

    def test_rot_iunknown_is_promoted_to_idispatch(self) -> None:
        try:
            import win32com.client.dynamic  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("pywin32 is only present in the packaged COM runtime")

        class Unknown:
            def QueryInterface(self, iid):
                self.iid = iid
                return "idispatch"

        unknown = Unknown()
        with patch("win32com.client.dynamic.Dispatch", return_value="dte") as dispatch:
            result = _com._dynamic_dispatch(unknown)
        self.assertEqual("dte", result)
        self.assertIsNotNone(unknown.iid)
        dispatch.assert_called_once_with("idispatch")

    def test_manifest_validation_is_offline_and_native(self) -> None:
        configuration = {
            "schema_version": 1,
            "master": {"name": "Device 1 (EtherCAT)", "subtype": 111},
            "devices": [],
        }
        with patch("tc_template.io_native.get_active_dte") as connect:
            result = io_native.execute("check-manifest", configuration=configuration)
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["validation"]["ok"])
        connect.assert_not_called()

    def test_config_command_strategy_uses_legacy_name_when_no_commands_are_enumerable(self) -> None:
        class Dte:
            def __init__(self):
                self.commands = []

            def ExecuteCommand(self, command):
                self.commands.append(command)

        dte = Dte()
        result = _native_bridge._set_config_mode(dte, {"strategy": "command"})
        self.assertEqual(["TwinCAT.RestartTwinCATConfigMode"], dte.commands)
        self.assertEqual("TwinCAT.RestartTwinCATConfigMode", result["strategy"])

    def test_config_command_strategy_discovers_current_xae_command(self) -> None:
        class Command:
            Name = "TwinCAT.RestartToConfig"

        class Commands:
            Count = 1

            @staticmethod
            def Item(index):
                assert index == 1
                return Command()

        class Dte:
            def __init__(self):
                self.Commands = Commands()
                self.commands = []

            def ExecuteCommand(self, command):
                self.commands.append(command)
                if command == "TwinCAT.RestartTwinCATConfigMode":
                    raise RuntimeError("invalid command")

        dte = Dte()
        result = _native_bridge._set_config_mode(dte, {"strategy": "command"})
        self.assertEqual(
            ["TwinCAT.RestartTwinCATConfigMode", "TwinCAT.RestartToConfig"],
            dte.commands)
        self.assertEqual("TwinCAT.RestartToConfig", result["strategy"])

    def test_realtime_refresh_uses_dte_command_without_selecting_a_page(self) -> None:
        class Dte:
            def __init__(self):
                self.commands = []

            def ExecuteCommand(self, command):
                self.commands.append(command)

        dte = Dte()
        result = _native_bridge._realtime_refresh(dte)
        self.assertEqual(["TwinCAT.刷新"], dte.commands)
        self.assertEqual("refreshed", result["status"])
        self.assertFalse(result["focus_changed"])
        self.assertFalse(result["configuration_changed"])

    def test_system_remove_native_persists_when_applied(self) -> None:
        class Dte:
            def __init__(self):
                self.commands = []

            def ExecuteCommand(self, command):
                self.commands.append(command)

        dte = Dte()
        with patch.object(_native_bridge, "_system_manager", return_value=object()), \
             patch.object(_native_bridge.tc_platform, "system_remove",
                          return_value={"status": "deleted", "path": "TIRT^PlcTask1",
                                        "applied": True}) as remove:
            result = _native_bridge._system_remove_and_save(
                dte, {"path": "TIRT^PlcTask1", "apply": True})
        remove.assert_called_once()
        self.assertEqual(["File.SaveAll"], dte.commands)
        self.assertTrue(result["saved"])

    def test_io_master_filter_uses_twincat_device_item_type(self) -> None:
        class Node:
            def __init__(self, name, item_type):
                self.Name = name
                self.ItemType = item_type

        nodes = [Node("Image", 5), Node("Inputs", 3),
                 Node("EtherCAT Master", 2), Node("Term 1", 3)]
        self.assertEqual(["EtherCAT Master"],
                         [node.Name for node in io_native._io_masters(nodes)])

    def test_empty_io_tree_allows_first_master_creation(self) -> None:
        class Node:
            def __init__(self, name, item_type, children=()):
                self.Name = name
                self.ItemType = item_type
                self.children = list(children)

            def __iter__(self):
                return iter(self.children)

            def CreateChild(self, name, subtype, before, info):
                created = Node(name, 2)
                created.ItemSubType = subtype
                self.children.append(created)
                return created

        root = Node("I/O", 1, [Node("Image", 5), Node("Inputs", 3)])
        sysman = SimpleNamespace(LookupTreeItem=lambda path: root)
        data = {"schema_version": 1,
                "master": {"name": "Device 1 (EtherCAT)", "subtype": 111},
                "devices": []}
        dte = SimpleNamespace(
            Solution=SimpleNamespace(FullName="empty-io.sln"),
            ExecuteCommand=lambda command: None,
        )
        with patch("tc_template.io_native._sysman", return_value=sysman):
            result = io_native.create_configuration(data, dte)
        self.assertEqual("$master", result["created"][0]["id"])
        self.assertEqual(["Device 1 (EtherCAT)"],
                         [node.Name for node in io_native._io_masters(root)])

    def test_empty_io_export_returns_empty_instead_of_error(self) -> None:
        class Node:
            def __init__(self, name, item_type):
                self.Name = name
                self.ItemType = item_type

            def __iter__(self):
                return iter([Node("Image", 5), Node("Outputs", 3)])

        sysman = SimpleNamespace(LookupTreeItem=lambda path: Node("I/O", 1))
        with patch("tc_template.io_native._sysman", return_value=sysman):
            result = io_native.export_configuration(object())
        self.assertTrue(result["empty"])
        self.assertEqual(0, result["master_count"])

    def test_customer_build_contains_pywin32_and_excludes_ps1(self) -> None:
        portable = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        installer = (ROOT / "scripts" / "build_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"pywin32_system32"', portable)
        self.assertIn("python-3.12.10-embed-win32.zip", portable)
        self.assertIn('"runtime\\com32"', portable)
        self.assertIn("pywin32-312-cp312-cp312-win32.whl", portable)
        self.assertIn('(Join-Path $comSp "pyads")', portable)
        self.assertIn("32 位 COM helper 无法导入 pyads", portable)
        self.assertIn("prepare_native_runtime", portable)
        self.assertIn('/XF "*.pyc" "*.ps1"', portable)
        self.assertIn('"Start-Backend.vbs", "Start-Backend.cmd", "README.txt"', portable)
        self.assertIn("|\\.ps1$", installer)

    def test_native_worker_resolves_matching_ads_runtime(self):
        portable = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        worker = (ROOT / "tc_template" / "_native_worker.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"Common32"', worker)
        self.assertIn('"Common64"', worker)
        self.assertIn('"SysWOW64"', worker)
        self.assertIn('"System32"', worker)
        self.assertIn("64 位 COM helper 无法导入", portable)


if __name__ == "__main__":
    unittest.main()
