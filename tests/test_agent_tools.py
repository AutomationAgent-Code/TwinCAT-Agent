from __future__ import annotations

import json
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_agent import agent_core
from tc_agent.ads import AdsStateError, _coerce_ads_scalar, _parse_net_id
from tc_agent.plc_value_validation import (
    ValueNormalizationError, scalar_values_match,
)
from tc_template._ps_bridge import ps_io_configuration


class AgentToolRegistryTests(unittest.TestCase):
    def test_every_tool_has_versioned_execution_metadata(self) -> None:
        for tool in agent_core.REGISTRY:
            metadata = agent_core.tool_metadata(tool["name"])
            self.assertEqual(1, metadata["protocol_version"])
            self.assertIn(metadata["idempotency"], {"safe_replay", "ledger_guarded"})
            if metadata["readonly"]:
                self.assertEqual("none", metadata["side_effect"])
                self.assertEqual("safe_replay", metadata["idempotency"])
            else:
                self.assertNotEqual("none", metadata["side_effect"])
                self.assertEqual("ledger_guarded", metadata["idempotency"])

    def test_project_authoring_tools_are_exposed(self) -> None:
        names = [tool["name"] for tool in agent_core.REGISTRY]
        self.assertEqual(len(names), len(set(names)), "tool names must be unique")
        self.assertEqual(209, len(names))
        self.assertIn("plc_editor_state", names)
        self.assertIn('tc_create_solution', names)
        self.assertIn('tc_solution_templates', names)
        self.assertTrue({
            "plc_create_project",
            "plc_create_folder",
            "plc_remove_project",
            "plc_delete_project",
            "plc_delete_member",
            "plc_rename_member",
            "plc_restore_snapshot",
            "plc_git_sync",
            "tc_projects",
            "tc_project_info",
            "tc_hmi_create_project",
            "tc_hmi_project_info",
            "tc_hmi_structure",
            "tc_hmi_read",
            "tc_hmi_read_smart",
            "tc_hmi_control_schema",
            "tc_hmi_source_catalog",
            "tc_hmi_source_index",
            "tc_hmi_write_markup",
            "tc_hmi_ads_info",
            "tc_hmi_validate",
            "tc_hmi_create_view",
            "tc_hmi_project_api",
            "tc_hmi_startup_view_set",
            "tc_hmi_control_edit",
            "tc_hmi_controls_batch",
            "tc_hmi_delete_view",
            "tc_hmi_ads_runtime_set",
            "tc_hmi_ads_symbols",
            "tc_hmi_ads_symbol_set",
            "tc_hmi_dynamic_symbols_set",
            "tc_hmi_bind_plc",
            "tc_hmi_bindings",
            "tc_hmi_internal_symbols",
            "tc_hmi_internal_symbol_set",
            "tc_hmi_localizations",
            "tc_hmi_localization_set",
            "tc_hmi_themes",
            "tc_hmi_themed_resource_set",
            "tc_hmi_active_theme_set",
            "tc_hmi_user_controls",
            "tc_hmi_user_control_create",
            "tc_hmi_user_control_parameter_set",
            "tc_hmi_user_control_delete",
            "tc_hmi_framework_templates",
            "tc_hmi_framework_validate",
            "tc_hmi_framework_control_info",
            "tc_hmi_framework_attribute_set",
            "tc_hmi_framework_event_set",
            "tc_hmi_framework_create",
            "tc_hmi_framework_pack",
            "tc_hmi_framework_packages",
            "tc_hmi_framework_package_inspect",
            "tc_hmi_framework_install",
            "tc_hmi_framework_uninstall",
            "tc_hmi_runtime_info",
            "tc_hmi_server_control",
            "tc_hmi_ads_live_check",
            "tc_hmi_binding_diagnose",
            "tc_hmi_browser_validate",
            "tc_hmi_build",
            "tc_open",
            "tc_close",
            "plc_vars",
            "plc_find",
            "plc_search",
            "plc_review",
            "plc_static_analysis",
            "plc_tree",
            "plc_coding_profile",
            "plc_coding_profile_set",
            "fblib_find",
            "fblib_add",
            "plc_generate",
            "plc_create_standard_fb",
            "agent_report_create",
            "plc_create_property",
            "plc_patch",
            "plc_snapshot",
            "plc_snapshots",
            "plc_changed",
            "plc_diff",
            "plc_import_plcopen",
            "plc_export_plcopen",
            "tc_system_structure",
            "tc_system_settings",
            "tc_system_settings_set",
            "tc_core_info",
            "tc_core_assign",
            "tc_realtime_info",
            "tc_realtime_validate",
            "tc_realtime_settings_set",
            "tc_task_info",
            "tc_task_runtime_info",
            "tc_task_core_assign",
            "tc_task_settings_set",
            "tc_system_add",
            "tc_system_remove",
            "tc_realtime_refresh",
            "tc_safety_structure",
            "tc_safety_project_info",
            "tc_safety_files",
            "tc_safety_target_info",
            "tc_safety_aliases",
            "tc_safety_application",
            "tc_safety_logic_check",
            "tc_safety_validate",
            "tc_safety_import",
            "tc_safety_create",
            "tc_safety_export",
            "tc_safety_remove",
            "tc_safety_delete",
            "thread_list",
            "thread_send",
            "thread_inbox",
            "thread_rename",
            "thread_fork",
            "memory_list",
            "memory_remember",
            "memory_forget",
            "project_data_health",
            "project_data_backup",
            "project_data_export",
            "plc_read_values",
            "plc_write_values",
            "plc_read_value",
            "plc_write_value",
            "plc_verify",
            "plc_read_fast",
            "plc_read_smart",
            "plc_source_catalog",
            "plc_read_current",
            "plc_dirty_current",
        }.issubset(names))
        self.assertTrue({
            "thread_create", "thread_collect", "thread_wait", "thread_handoff"
        }.isdisjoint(names))

    def test_plc_read_fast_uses_one_batch_call_and_suppresses_known_source(self) -> None:
        implementation = "nCount := nCount + 1;"
        digest = __import__("hashlib").sha256(implementation.encode("utf-8")).hexdigest()
        with patch.object(agent_core, "ps_com", return_value={
            "status": "read", "results": [{
                "name": "MAIN", "declaration": "PROGRAM MAIN",
                "implementation": implementation,
            }],
        }) as call:
            result = agent_core._plc_read_fast({
                "requests": [{
                    "id": "main", "name": "MAIN", "area": "all",
                    "known_hashes": {"implementation": digest},
                }],
                "live": True,
            })
        call.assert_called_once()
        self.assertEqual("read-batch", call.call_args.args[0])
        self.assertEqual("", result["results"][0]["implementation"])
        self.assertIn("implementation", result["results"][0]["unchanged_areas"])
        self.assertEqual("single-helper-single-dte-live-batch", result["strategy"])

    def test_plc_read_fast_limits_batch_to_context_safe_source_size(self) -> None:
        with patch.object(agent_core, "ps_com", return_value={"status": "read", "results": []}) as call:
            agent_core._plc_read_fast({
                "requests": [{"name": "MAIN"}], "max_total_chars": 200000, "live": True,
            })
        self.assertEqual(agent_core.PLC_BATCH_MAX_TOTAL_CHARS,
                         call.call_args.kwargs["max_total_chars"])

    def test_plc_read_fast_defaults_to_one_saved_source_index_batch(self) -> None:
        indexed = [{"name": "MAIN", "implementation": "n := 1;", "hashes": {}}]
        with patch.object(agent_core, "ps_com", return_value={"solution": r"C:\P\P.sln"}) as com, \
             patch.object(agent_core, "read_many_indexed_source", return_value=indexed) as indexed_read:
            result = agent_core._plc_read_fast({"requests": [{"name": "MAIN"}]})
        com.assert_called_once_with("project-info")
        indexed_read.assert_called_once()
        self.assertEqual("single-index-sync-saved-source-batch", result["strategy"])
        self.assertFalse(result["live_xae"])

    def test_plc_read_smart_live_reads_complete_areas(self) -> None:
        with patch.object(agent_core, "ps_com", return_value={
            "name": "MAIN", "declaration": "PROGRAM MAIN",
            "implementation": "\n".join(f"line {i}" for i in range(600)),
        }) as call:
            result = agent_core._plc_read_smart({"name": "MAIN", "live": True})
        call.assert_called_once()
        self.assertEqual("read-pou", call.call_args.args[0])
        self.assertEqual(0, call.call_args.kwargs["max_lines"])
        self.assertEqual("live_com", result["source"])
        self.assertIn("implementation", result["hashes"])

    def test_plc_read_current_uses_one_current_document_call(self) -> None:
        with patch.object(agent_core, "ps_com", return_value={
            "name": "MAIN", "declaration": "PROGRAM MAIN", "implementation": "n := 1;",
            "active_document": {"saved": False, "source_file": r"C:\\PLC\\MAIN.TcPOU"},
        }) as call:
            result = agent_core._plc_read_current({})
        call.assert_called_once()
        self.assertEqual("read-current", call.call_args.args[0])
        self.assertFalse(result["active_document"]["saved"])
        self.assertEqual("live_com", result["source"])

    def test_plc_dirty_current_compares_only_active_file(self) -> None:
        live = {"name": "MAIN", "declaration": "PROGRAM MAIN", "implementation": "n := 2;",
                "hashes": {}, "active_document": {"saved": False,
                "source_file": r"C:\\PLC\\MAIN.TcPOU", "member": ""}}
        disk = {"declaration": "PROGRAM MAIN", "implementation": "n := 1;", "hashes": {}}
        with patch.object(agent_core, "_plc_read_current", return_value=live), \
             patch.object(agent_core, "read_source_file", return_value=disk) as disk_call:
            result = agent_core._plc_dirty_current({})
        disk_call.assert_called_once_with(r"C:\\PLC\\MAIN.TcPOU", "MAIN", member="", area="all")
        self.assertTrue(result["dirty"])
        self.assertEqual(["implementation"], [item["area"] for item in result["differences"]])

    def test_plc_verify_workflow_runs_fixed_stages_and_runtime_assertions(self) -> None:
        build = {"failedProjects": 0, "errorCount": 0, "errors": [],
                 "buildPerformed": True, "diagnosticsAvailable": True}
        analysis = {"summary": {"errors": 0, "warnings": 2}}
        runtime = {"verified": True, "assertion_count": 1}
        build_result = {**build, "compiler_verified": True,
                        "diagnostics_complete": True}
        with patch.object(agent_core, "execute_plc_build", return_value=build_result) as compile_call, \
             patch.object(agent_core, "ps_com", return_value=[{"name": "MAIN"}]) as com_call, \
             patch.object(agent_core, "analyze_objects", return_value=analysis), \
             patch.object(agent_core, "_plc_read_values", return_value=runtime) as values:
            result = agent_core._plc_verify_workflow({
                "symbols": [{"name": "MAIN.bReady", "type": "BOOL", "expected": True}],
                "runtime": "PLC1",
            })
        self.assertFalse(result["verified"])
        self.assertTrue(result["source_verified"])
        self.assertTrue(result["runtime_values_verified"])
        self.assertIsNone(result["runtime_matches_build"])
        self.assertEqual("incomplete", result["status"])
        self.assertEqual(
            ["source_calibration", "build", "static_analysis", "runtime_assertions"],
            [stage["stage"] for stage in result["stages"]],
        )
        compile_call.assert_called_once()
        com_call.assert_called_once_with("all-code")
        self.assertEqual("PLC1", values.call_args.args[0]["runtime"])

    def test_plc_verify_skips_runtime_when_build_fails(self) -> None:
        with patch.object(
            agent_core, "execute_plc_build",
            return_value={"failedProjects": 1, "errorCount": 1,
                          "compiler_verified": False},
        ), patch.object(agent_core, "ps_com", return_value=[]), \
             patch.object(agent_core, "analyze_objects", return_value={
                 "summary": {"errors": 0, "warnings": 0}
             }), patch.object(agent_core, "_plc_read_values") as values:
            result = agent_core._plc_verify_workflow({
                "symbols": [{"name": "MAIN.bReady", "type": "BOOL", "expected": True}],
            })
        self.assertFalse(result["verified"])
        self.assertTrue(result["stages"][-1]["skipped"])
        values.assert_not_called()

    def test_nc_tools_are_exposed_with_safe_permissions(self) -> None:
        names = {tool["name"] for tool in agent_core.REGISTRY}
        readonly = {
            "nc_structure", "nc_axis_info", "nc_axis_params", "nc_drive_list",
            "nc_encoder_list", "nc_links", "nc_state", "nc_axis_state",
            "nc_validate",
        }
        mutations = {"nc_create_task", "nc_create_axis", "nc_link_drive", "nc_link_encoder"}
        self.assertTrue((readonly | mutations).issubset(names))
        for name in readonly:
            self.assertTrue(agent_core.is_readonly(name))
            self.assertEqual("allow", agent_core.decide("plan", name))
        for name in mutations:
            self.assertEqual("ask", agent_core.decide("accept", name))
            self.assertEqual("deny", agent_core.decide("plan", name))

    def test_target_and_io_tools_are_exposed(self) -> None:
        names = {tool["name"] for tool in agent_core.REGISTRY}
        self.assertTrue({
            "tc_target_show",
            "tc_target_routes",
            "tc_target_set",
            "tc_io_structure",
            "tc_io_manifest_check",
            "tc_io_esi_check",
            "tc_io_validate",
            "tc_io_create",
            "tc_io_remove",
            "tc_io_export",
            "tc_scan_devices",
        }.issubset(names))

    def test_project_destructive_tools_still_require_confirmation(self) -> None:
        self.assertEqual("allow", agent_core.decide("accept", "plc_create_project"))
        self.assertEqual("ask", agent_core.decide("accept", "plc_remove_project"))
        self.assertEqual("ask", agent_core.decide("accept", "plc_delete_project"))
        self.assertEqual("ask", agent_core.decide("accept", "tc_open"))
        self.assertEqual("ask", agent_core.decide("accept", "tc_close"))
        self.assertEqual("deny", agent_core.decide("plan", "plc_create_project"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_remove_project"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_delete_project"))

    def test_readonly_project_queries_are_always_allowed(self) -> None:
        for name in (
            "tc_projects", "tc_project_info", "plc_vars", "plc_find", "plc_search",
            "plc_static_analysis",
            "plc_coding_profile", "fblib_find", "plc_generate",
            "plc_snapshots", "plc_changed", "plc_diff",
            "tc_target_show", "tc_target_routes", "tc_io_structure",
            "tc_io_manifest_check", "tc_io_esi_check", "tc_io_validate",
        ):
            with self.subTest(name=name):
                self.assertTrue(agent_core.is_readonly(name))
                self.assertEqual("allow", agent_core.decide("plan", name))

    def test_generate_standard_fb_uses_profile_before_writing(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(
            agent_core, "_active_solution", return_value="G:/Project/Test.sln"
        ), patch.object(
            agent_core.coding_profile, "load_profile",
            return_value={"author": "Ethan", "indent_spaces": 4,
                          "comment_language": "Simplified Chinese",
                          "extra_rules": [], "path": "profile.json"},
        ):
            generated = registry["plc_generate"]["run"]({
                "name": "Motor", "purpose": "控制电机启停", "mode": "transaction",
            })
        self.assertEqual("FB_Motor", generated["fb_name"])
        self.assertIn("作者：Ethan", generated["fb_declaration"])
        self.assertIn("bDone", generated["fb_declaration"])
        self.assertIn("CASE eState OF", generated["fb_implementation"])

    def test_raw_standard_fb_create_is_redirected_before_com(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call:
            result = registry["plc_create"]["run"]({
                "name": "FB_Motor", "type": "fb",
                "declaration": "FUNCTION_BLOCK FB_Motor",
                "implementation": "",
            })
        self.assertEqual("blocked", result["status"])
        self.assertIn("plc_create_standard_fb", result["recommended_tools"])
        call.assert_not_called()

    def test_plc_create_and_folder_tools_preserve_exact_tree_paths(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        parent = "TIPC^PLC1^PLC1 Project^POUs^00_Local"
        with patch.object(agent_core, "ps_com", return_value={"status": "ok"}) as call:
            created = registry["plc_create"]["run"]({
                "name": "PRG_Nested", "type": "program", "path": parent,
            })
            folder = registry["plc_create_folder"]["run"]({
                "name": "02_Auto", "parent_path": parent,
            })
        self.assertEqual("created", created["status"])
        self.assertEqual("ok", folder["status"])
        self.assertEqual(parent, call.call_args_list[0].kwargs["path"])
        self.assertEqual(parent, call.call_args_list[1].kwargs["parent_path"])

    def test_create_standard_fb_generates_then_creates_enum_and_fb(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        profile = {"author": "Agent", "indent_spaces": 4,
                   "comment_language": "Simplified Chinese", "extra_rules": [], "path": ""}
        with patch.object(agent_core, "_active_solution", return_value="demo.sln"), \
             patch.object(agent_core.coding_profile, "load_profile", return_value=profile), \
             patch.object(agent_core, "ps_com") as call:
            call.side_effect = [
                {"matches": [], "total": 0},
                {"status": "created"}, {"status": "created"},
            ]
            result = registry["plc_create_standard_fb"]["run"]({
                "name": "AxisHome", "purpose": "执行单次回零", "mode": "transaction",
            })
        self.assertEqual("created", result["status"])
        self.assertEqual(
            ["find-pou", "new-pou", "new-pou"],
            [c.args[0] for c in call.call_args_list],
        )
        self.assertEqual("enum", call.call_args_list[1].kwargs["type"])
        self.assertEqual("fb", call.call_args_list[2].kwargs["type"])

    def test_create_standard_fb_reuses_existing_enum(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        profile = {"author": "Agent", "indent_spaces": 4,
                   "comment_language": "Simplified Chinese", "extra_rules": [], "path": ""}
        with patch.object(agent_core, "_active_solution", return_value="demo.sln"), \
             patch.object(agent_core.coding_profile, "load_profile", return_value=profile), \
             patch.object(agent_core, "ps_com") as call:
            call.side_effect = [
                {"matches": [{"name": "E_AxisHomeState", "itemType": 605}], "total": 1},
                {"status": "created"},
            ]
            result = registry["plc_create_standard_fb"]["run"]({
                "name": "AxisHome", "purpose": "执行单次回零", "mode": "transaction",
            })
        self.assertEqual("existing", result["enum"]["status"])
        self.assertEqual(["find-pou", "new-pou"], [c.args[0] for c in call.call_args_list])

    def test_raw_property_member_creation_redirects_to_high_level_tool(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call:
            result = registry["plc_create_member"]["run"]({
                "pou": "FB_Motor", "name": "State",
                "member_type": "property", "return_type": "E_State",
            })
        self.assertEqual("blocked", result["status"])
        self.assertEqual("plc_create_property", result["recommended_tool"])
        call.assert_not_called()

    def test_create_property_reuses_get_and_creates_missing_set(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        structure = {
            "methods": [{"name": "State", "itemType": 611,
                         "members": [{"name": "Get", "itemType": 613}]}],
        }
        with patch.object(agent_core, "ps_com") as call:
            call.side_effect = [
                {'declaration': 'FUNCTION_BLOCK FB_Motor\nVAR eState : BOOL; END_VAR',
                 'path': 'TIPC^PLC1^Project^POUs^FB_Motor'},
                {"status": "created", "type": "property"}, structure,
                {"status": "written"}, {"status": "created", "type": "propset"},
            ]
            result = registry["plc_create_property"]["run"]({
                "pou": "FB_Motor", "name": "State", "return_type": "BOOL",
                "getter_implementation": "State := eState;",
                "setter_implementation": "eState := State;",
            })
        self.assertEqual("created", result["status"])
        self.assertEqual("write-pou", call.call_args_list[3].args[0])
        self.assertEqual("State.Get", call.call_args_list[3].kwargs["method"])
        self.assertEqual("new-member", call.call_args_list[4].args[0])
        self.assertEqual("propset", call.call_args_list[4].kwargs["type"])

    def test_static_analysis_reads_all_code_without_writing(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        source = [{"name": "PRG_Main", "declaration": "PROGRAM PRG_Main", "implementation": ""}]
        with patch.object(agent_core, "ps_com", return_value=source) as call:
            result = registry["plc_static_analysis"]["run"]({})
        self.assertEqual("TCSA", result["engine"])
        self.assertFalse(result["te1200_executed"])
        call.assert_called_once_with("all-code")

    def test_plc_read_normalizes_member_tree_path_and_action_type(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "ok"}) as call:
            result = registry["plc_read"]["run"]({
                "name": "FB_Machine",
                "path": "TIPC^PLC1^CVT24007^POUs^FB_Machine^A_01_Auto",
                "member_type": "action",
                "max_lines": 40,
            })
        self.assertEqual({"status": "ok"}, result)
        call.assert_called_once_with(
            "read-pou", name="FB_Machine", area="all",
            path="TIPC^PLC1^CVT24007^POUs^FB_Machine",
            method="A_01_Auto", member_type="action",
            include_member_code=False, start_line=1, max_lines=40,
        )

    def test_large_project_patch_obeys_edit_permissions(self) -> None:
        self.assertEqual("deny", agent_core.decide("plan", "plc_patch"))
        self.assertEqual("allow", agent_core.decide("accept", "plc_patch"))
        self.assertEqual("deny", agent_core.decide("plan", "plc_snapshot"))
        self.assertEqual("allow", agent_core.decide("accept", "plc_snapshot"))

    def test_runtime_value_tools_have_safe_permissions(self) -> None:
        self.assertTrue(agent_core.is_readonly("plc_read_values"))
        self.assertEqual("allow", agent_core.decide("plan", "plc_read_values"))
        self.assertEqual("deny", agent_core.decide("plan", "plc_write_values"))
        self.assertEqual("ask", agent_core.decide("accept", "plc_write_values"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_write_values"))
        self.assertTrue(agent_core.is_readonly("plc_read_value"))
        self.assertEqual("allow", agent_core.decide("plan", "plc_read_value"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_write_value"))

    def test_plc_read_value_uses_dynamic_symbol_type(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state",
                          return_value={"state_code": 5, "state_name": "Run"}), \
             patch.object(agent_core, "read_dynamic_ads_symbol", return_value={
                 "value": 7, "symbol": {"type": "INT", "byte_size": 2},
             }) as read:
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_read_value"]["run"]({
                "name": "MAIN.nValue", "expected": 7,
            })
        self.assertTrue(result["verified"])
        self.assertEqual("INT", result["metadata"]["type"])
        read.assert_called_once_with("1.2.3.4.1.1", 851, "MAIN.nValue", depth=3)

    def test_task_runtime_info_reports_unexported_system_symbols(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state",
                          return_value={"state_code": 5, "state_name": "Run"}), \
             patch.object(agent_core, "read_dynamic_ads_symbol",
                          side_effect=agent_core.DynamicAdsError("symbol not found")):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
                {"tasks": [{"name": "PlcTask"}]},
            ]
            result = registry["tc_task_runtime_info"]["run"]({})
        self.assertEqual("unavailable", result["status"])
        self.assertFalse(result["available"])
        self.assertEqual(851, result["ads_port"])

    def test_realtime_validate_tool_is_read_only_and_uses_snapshot(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        snapshot = {
            "twincat": {"family": "4026"},
            "memory": {"router_memory_mb": 32, "max_task_stack_kb": 64},
            "cores": [{"id": 0, "selected": True, "configured": True,
                       "base_time_100ns": 10000, "load_limit_percent": 80}],
            "tasks": [{"name": "PlcTask", "priority": 20,
                       "cycle_time_100ns": 100000, "cpu_affinity": 1}],
        }
        with patch.object(agent_core, "ps_com", return_value=snapshot):
            result = registry["tc_realtime_validate"]["run"]({})
        self.assertTrue(result["valid"])
        self.assertIs(result["snapshot"], snapshot)

    def test_plc_write_value_reads_before_and_verifies(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state",
                          return_value={"state_code": 5, "state_name": "Run"}), \
             patch.object(agent_core, "read_dynamic_ads_symbol",
                          return_value={"value": 5}), \
             patch.object(agent_core, "write_dynamic_ads_symbol", return_value={
                 "before": 5, "readback": 6, "symbol": {"type": "INT"},
             }) as write:
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_write_value"]["run"]({
                "name": "MAIN.nValue", "value": 6, "expected_before": 5,
            })
        self.assertEqual("verified", result["status"])
        self.assertTrue(result["verified"])
        write.assert_called_once_with("1.2.3.4.1.1", 851, "MAIN.nValue", 6, depth=1)

    def test_dynamic_real_string_input_matches_real_readback(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state", return_value={"state_code": 5}), \
             patch.object(agent_core, "read_dynamic_ads_symbol", return_value={
                 "value": 75.0, "symbol": {"type": "REAL", "category": "Primitive"},
             }), \
             patch.object(agent_core, "write_dynamic_ads_symbol", return_value={
                 "readback": 75.0, "symbol": {"type": "REAL", "category": "Primitive"},
             }) as write:
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_write_value"]["run"]({
                "name": "MAIN.rManualValue", "value": "75.0",
            })
        self.assertEqual("verified", result["status"])
        self.assertTrue(result["written"])
        self.assertTrue(result["verified"])
        self.assertEqual(75.0, result["normalized_expected"])
        write.assert_called_once_with("1.2.3.4.1.1", 851,
                                      "MAIN.rManualValue", 75.0, depth=1)

    def test_dynamic_real_storage_rounding_and_lreal_precision(self) -> None:
        real_stored = ctypes.c_float(0.1).value
        self.assertTrue(agent_core._dynamic_values_match(
            real_stored, "0.1", None, {"type": "REAL", "category": "Primitive"}
        )[0])
        self.assertTrue(agent_core._dynamic_values_match(
            0.1, "0.1", None, {"type": "LREAL", "category": "Primitive"}
        )[0])
        self.assertFalse(agent_core._dynamic_values_match(
            0.1 + 1e-12, "0.1", None, {"type": "LREAL", "category": "Primitive"}
        )[0])

    def test_typed_comparison_preserves_64_bit_integer_and_bool_rules(self) -> None:
        largest = 2**63 - 1
        self.assertTrue(agent_core._plc_values_match(largest, largest, "lint"))
        self.assertFalse(agent_core._plc_values_match(float(largest), largest, "lint"))
        self.assertTrue(agent_core._plc_values_match(True, "true", "bool"))
        self.assertFalse(agent_core._plc_values_match(False, "yes", "bool"))
        with self.assertRaises(ValueNormalizationError):
            scalar_values_match(1, "nan", "REAL")

    def test_dynamic_string_enum_aggregate_and_unknown_types_are_not_coerced(self) -> None:
        self.assertTrue(agent_core._dynamic_values_match(
            "75.0", "75.0", None, {"type": "STRING", "category": "Primitive"}
        )[0])
        self.assertFalse(agent_core._dynamic_values_match(
            75, "75.0", None, {"type": "STRING", "category": "Primitive"}
        )[0])
        self.assertTrue(agent_core._dynamic_values_match(
            "Running", "Running", None, {"type": "E_State", "category": "Enum"}
        )[0])
        self.assertTrue(agent_core._dynamic_values_match(
            [1, {"x": True}], [1, {"x": True}], None,
            {"type": "ARRAY [0..1] OF ST_X", "category": "Array"}
        )[0])
        with self.assertRaises(ValueNormalizationError):
            agent_core._dynamic_values_match(1, 1, None, {"type": "VendorType"})

    def test_dynamic_write_preserves_mismatch_and_readback_unavailable_states(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        base = {"target_netid": "1.2.3.4.1.1"}
        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state", return_value={"state_code": 5}), \
             patch.object(agent_core, "read_dynamic_ads_symbol", return_value={
                 "value": 1.0, "symbol": {"type": "REAL", "category": "Primitive"},
             }), \
             patch.object(agent_core, "write_dynamic_ads_symbol", return_value={
                 "readback": 2.0, "symbol": {"type": "REAL", "category": "Primitive"},
             }):
            call.side_effect = [base, {"plcs": [{"name": "PLC1", "ads_port": 851}]}]
            mismatch = registry["plc_write_value"]["run"]({
                "name": "MAIN.rValue", "value": 1.0,
            })
        self.assertEqual("readback_mismatch", mismatch["status"])
        self.assertTrue(mismatch["written"])
        self.assertFalse(mismatch["verified"])
        self.assertFalse(mismatch["retry_safe"])

        with patch.object(agent_core, "ps_com") as call, \
             patch.object(agent_core, "read_ads_state", return_value={"state_code": 5}), \
             patch.object(agent_core, "read_dynamic_ads_symbol", return_value={
                 "value": 1.0, "symbol": {"type": "REAL", "category": "Primitive"},
             }), \
             patch.object(agent_core, "write_dynamic_ads_symbol", return_value={
                 "readback_error": "ADS read failed",
                 "symbol": {"type": "REAL", "category": "Primitive"},
             }):
            call.side_effect = [base, {"plcs": [{"name": "PLC1", "ads_port": 851}]}]
            unavailable = registry["plc_write_value"]["run"]({
                "name": "MAIN.rValue", "value": 1.0,
            })
        self.assertEqual("written_readback_unavailable", unavailable["status"])
        self.assertTrue(unavailable["written"])
        self.assertFalse(unavailable["verified"])

    def test_dynamic_invalid_tolerance_does_not_write(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "write_dynamic_ads_symbol") as write:
            result = registry["plc_write_value"]["run"]({
                "name": "MAIN.rValue", "value": 1.0, "tolerance": float("nan"),
            })
        self.assertEqual("invalid_tolerance", result["status"])
        self.assertFalse(result["written"])
        write.assert_not_called()

    def test_plc_read_values_asserts_live_values(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         return_value={"state_code": 5, "state_name": "Run"}),
            patch.object(agent_core, "read_ads_values_by_name",
                         return_value={"MAIN.bReady": True, "MAIN.lrValue": 1.25}),
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_read_values"]["run"]({"symbols": [
                {"name": "MAIN.bReady", "type": "BOOL", "expected": True},
                {"name": "MAIN.lrValue", "type": "LREAL",
                 "expected": 1.25001, "tolerance": 0.001},
            ]})
        self.assertTrue(result["verified"])
        self.assertEqual(851, result["ads_port"])
        self.assertTrue(all(item["matched"] for item in result["values"]))

    def test_plc_write_values_reads_before_and_verifies_readback(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         return_value={"state_code": 5, "state_name": "Run"}),
            patch.object(agent_core, "read_ads_values_by_name") as read,
            patch.object(agent_core, "write_ads_values_by_name",
                         return_value={"MAIN.nSetpoint": 12}) as write,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            read.side_effect = [{"MAIN.nSetpoint": 10}, {"MAIN.nSetpoint": 12}]
            result = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.nSetpoint", "type": "INT", "value": 12,
                "expected_before": 10,
            }]})
        self.assertEqual("verified", result["status"])
        self.assertTrue(result["written"])
        self.assertTrue(result["verified"])
        write.assert_called_once_with(
            "1.2.3.4.1.1", 851, {"MAIN.nSetpoint": ("int", 12)})

    def test_plc_write_values_preserves_typed_normalization_and_mismatch_state(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         return_value={"state_code": 5, "state_name": "Run"}),
            patch.object(agent_core, "read_ads_values_by_name") as read,
            patch.object(agent_core, "write_ads_values_by_name",
                         return_value={"MAIN.rValue": 0.1}) as write,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            read.side_effect = [{"MAIN.rValue": 0.0}, {"MAIN.rValue": 0.1}]
            result = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.rValue", "type": "REAL", "value": "0.1",
            }]})
        self.assertEqual("verified", result["status"])
        self.assertTrue(result["verified"])
        self.assertEqual(ctypes.c_float(0.1).value,
                         result["values"][0]["normalized_expected"])
        write.assert_called_once_with(
            "1.2.3.4.1.1", 851, {"MAIN.rValue": ("real", 0.10000000149011612)})

        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         return_value={"state_code": 5, "state_name": "Run"}),
            patch.object(agent_core, "read_ads_values_by_name",
                         side_effect=[{"MAIN.n": 1}, {"MAIN.n": 2}]),
            patch.object(agent_core, "write_ads_values_by_name",
                         return_value={"MAIN.n": 2}),
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            mismatch = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.n", "type": "DINT", "value": 1,
            }]})
        self.assertEqual("written_readback_mismatch", mismatch["status"])
        self.assertTrue(mismatch["written"])
        self.assertFalse(mismatch["verified"])
        self.assertFalse(mismatch["retry_safe"])

    def test_plc_write_values_keeps_write_and_readback_errors_distinct(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state", return_value={"state_code": 5}),
            patch.object(agent_core, "read_ads_values_by_name",
                         side_effect=[{"MAIN.n": 1}, AdsStateError("ADS readback failed")]),
            patch.object(agent_core, "write_ads_values_by_name", return_value={"MAIN.n": 2}),
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.n", "type": "DINT", "value": 2,
            }]})
        self.assertEqual("written_readback_unavailable", result["status"])
        self.assertTrue(result["written"])
        self.assertFalse(result["verified"])

    def test_plc_write_values_rejects_invalid_tolerance_before_write(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "write_ads_values_by_name") as write:
            result = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.rValue", "type": "REAL", "value": 1.0,
                "tolerance": float("inf"),
            }]})
        self.assertEqual("invalid_tolerance", result["status"])
        self.assertFalse(result["written"])
        write.assert_not_called()

    def test_plc_write_values_stops_on_failed_precondition(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         return_value={"state_code": 5, "state_name": "Run"}),
            patch.object(agent_core, "read_ads_values_by_name",
                         return_value={"MAIN.nSetpoint": 11}),
            patch.object(agent_core, "write_ads_values_by_name") as write,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"plcs": [{"name": "PLC1", "ads_port": 851}]},
            ]
            result = registry["plc_write_values"]["run"]({"values": [{
                "name": "MAIN.nSetpoint", "type": "INT", "value": 12,
                "expected_before": 10,
            }]})
        self.assertEqual("precondition_failed", result["status"])
        self.assertFalse(result["written"])
        write.assert_not_called()

    def test_plc_read_values_reports_ads_offline_without_fabricating_values(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with (
            patch.object(agent_core, "ps_com") as call,
            patch.object(agent_core, "read_ads_state",
                         side_effect=AdsStateError("ADS 6")),
            patch.object(agent_core, "read_ads_values_by_name") as read,
        ):
            call.side_effect = [
                {"target_netid": "192.168.1.4.1.1"},
                {"plcs": [{"name": "PLC_Demo", "ads_port": 851}]},
            ]
            result = registry["plc_read_values"]["run"]({"symbols": [
                {"name": "MAIN.xResult", "type": "BOOL"},
            ]})
        self.assertEqual("offline", result["status"])
        self.assertFalse(result["verified"])
        self.assertEqual("ADS 6", result["error"])
        self.assertNotIn("values", result)
        read.assert_not_called()

    def test_ads_scalar_coercion_rejects_wraparound(self) -> None:
        self.assertEqual(255, _coerce_ads_scalar("USINT", 255))
        with self.assertRaises(AdsStateError):
            _coerce_ads_scalar("USINT", 256)
        with self.assertRaises(AdsStateError):
            _coerce_ads_scalar("UINT", -1)

    def test_plc_write_does_not_call_com_when_review_blocks(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "FB_Review", "path": "TIPC^PLC^POUs^FB_Review",
            "declaration": "FUNCTION_BLOCK FB_Review\nVAR_INPUT\nEND_VAR",
            "implementation": "", "methods": [],
        }
        with patch.object(agent_core, "ps_com", return_value=existing) as call:
            result = registry["plc_write"]["run"]({
                "name": "FB_Review", "area": "declaration",
                "code": "FUNCTION_BLOCK FB_Review\nVAR_INPUT\n    nSetpoint : INT;\nEND_VAR\nEND_FUNCTION_BLOCK",
            })
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["written"])
        call.assert_called_once()
        self.assertTrue(result["review"]["blocking_findings"])

    def test_plc_write_calls_com_only_after_review_approval(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "PRG_Main", "path": "TIPC^PLC^POUs^PRG_Main",
            "declaration": "PROGRAM PRG_Main\nVAR\n    nCount : UINT; // 计数\nEND_VAR",
            "implementation": "", "methods": [],
        }
        with patch.object(agent_core, "ps_com") as call:
            call.side_effect = [existing, {"status": "written"}, {
                **existing, "implementation": "nCount := nCount + 1;",
            }]
            result = registry["plc_write"]["run"]({
                "name": "PRG_Main", "area": "implementation", "code": "nCount := nCount + 1;",
            })
        self.assertEqual("written", result["status"])
        self.assertTrue(result["written"])
        self.assertEqual("write-pou", call.call_args_list[1].args[0])

    def test_full_plc_write_ignores_unchanged_historical_warnings(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "PRG_Main", "path": "TIPC^PLC^POUs^PRG_Main",
            "itemType": 602,
            "declaration": (
                "PROGRAM PRG_Main\nVAR\n"
                "    nOld : INT;\n"
                "    nKeep : INT;\nEND_VAR"
            ),
            "implementation": "", "methods": [],
        }
        replacement = (
            "PROGRAM PRG_Main\nVAR\n"
            "    nOld : INT;\n"
            "    nKeep : INT;\n"
            "    bNew : BOOL; // 新增变量\nEND_VAR"
        )
        with patch.object(agent_core, "ps_com") as call:
            call.side_effect = [existing, {"status": "written"}, {
                **existing, "declaration": replacement,
            }]
            result = registry["plc_write"]["run"]({
                "name": "PRG_Main", "area": "declaration", "code": replacement,
            })
        self.assertEqual("written", result["status"])
        self.assertTrue(result["review"]["delta_review"])
        self.assertFalse(result["review"]["blocking_findings"])
        self.assertGreaterEqual(len(result["review"]["advisories"]), 2)
        self.assertEqual("write-pou", call.call_args_list[1].args[0])

    def test_full_plc_write_allows_new_comment_warning(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "PRG_Main", "path": "TIPC^PLC^POUs^PRG_Main",
            "itemType": 602,
            "declaration": (
                "PROGRAM PRG_Main\nVAR\n"
                "    nOld : INT;\nEND_VAR"
            ),
            "implementation": "", "methods": [],
        }
        replacement = (
            "PROGRAM PRG_Main\nVAR\n"
            "    nOld : INT;\n"
            "    bNew : BOOL;\nEND_VAR"
        )
        with patch.object(agent_core, "ps_com", side_effect=[existing, {"status": "written"},
                          {**existing, "declaration": replacement}]) as call:
            result = registry["plc_write"]["run"]({
                "name": "PRG_Main", "area": "declaration", "code": replacement,
            })
        self.assertEqual("written", result["status"])
        self.assertFalse(result["review"]["blocking_findings"])
        self.assertIn("declaration-comment", {
            item["rule"] for item in result["review"]["advisories"]})
        self.assertEqual("write-pou", call.call_args_list[1].args[0])

    def test_plc_patch_does_not_block_on_unchanged_historical_error(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "FB_Review", "path": "TIPC^PLC^POUs^FB_Review",
            "itemType": 604,
            "declaration": (
                "FUNCTION_BLOCK FB_Review\nVAR_INPUT\n"
                "    bExecute : BOOL; // 执行命令\nEND_VAR\nVAR nValue : INT; // 值\nEND_VAR"
            ),
            "implementation": "bExecute := FALSE;\nnValue := 1;",
            "methods": [],
        }
        with patch.object(agent_core, "ps_com") as call:
            call.side_effect = [existing, {"status": "patched"}, {
                **existing, "implementation": "bExecute := FALSE;\nnValue := 2;",
            }]
            result = registry["plc_patch"]["run"]({
                "name": "FB_Review", "area": "implementation",
                "old_text": "nValue := 1;", "new_text": "nValue := 2;",
            })
        self.assertEqual("patched", result["status"])
        self.assertTrue(result["written"])
        self.assertTrue(result["review"]["delta_review"])
        self.assertFalse(result["review"]["blocking_findings"])
        self.assertIn("TCSA0037", {
            item["rule"] for item in result["review"]["historical_findings"]
        })
        self.assertEqual("patch-pou", call.call_args_list[1].args[0])

    def test_plc_patch_blocks_new_input_assignment(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "FB_Review", "path": "TIPC^PLC^POUs^FB_Review",
            "itemType": 604,
            "declaration": (
                "FUNCTION_BLOCK FB_Review\nVAR_INPUT\n"
                "    bExecute : BOOL; // 执行命令\nEND_VAR"
            ),
            "implementation": "nValue := 1;", "methods": [],
        }
        with patch.object(agent_core, "ps_com", return_value=existing) as call:
            result = registry["plc_patch"]["run"]({
                "name": "FB_Review", "area": "implementation",
                "old_text": "nValue := 1;", "new_text": "bExecute := FALSE;",
            })
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["written"])
        self.assertEqual(["TCSA0037"], [
            item["rule"] for item in result["review"]["blocking_findings"]
        ])
        call.assert_called_once()

    def test_plc_patch_blocks_worsened_duplicate_finding_count(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "FB_Review", "path": "TIPC^PLC^POUs^FB_Review",
            "itemType": 604,
            "declaration": (
                "FUNCTION_BLOCK FB_Review\nVAR_INPUT\n"
                "    bExecute : BOOL; // 执行命令\nEND_VAR"
            ),
            "implementation": "bExecute := FALSE;\nnValue := 1;", "methods": [],
        }
        with patch.object(agent_core, "ps_com", return_value=existing) as call:
            result = registry["plc_patch"]["run"]({
                "name": "FB_Review", "area": "implementation",
                "old_text": "nValue := 1;", "new_text": "bExecute := TRUE;",
            })
        self.assertEqual("blocked", result["status"])
        self.assertEqual(1, sum(
            item["rule"] == "TCSA0037"
            for item in result["review"]["blocking_findings"]
        ))
        self.assertEqual(1, sum(
            item["rule"] == "TCSA0037"
            for item in result["review"]["historical_findings"]
        ))
        call.assert_called_once()

    def test_interface_inline_member_write_is_blocked_before_com(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        existing = {
            "name": "I_Demo", "path": "TIPC^PLC^Interfaces^I_Demo",
            "itemType": 618, "declaration": "INTERFACE I_Demo",
            "implementation": "", "methods": [],
        }
        with patch.object(agent_core, "ps_com", return_value=existing) as call:
            result = registry["plc_write"]["run"]({
                "name": "I_Demo", "area": "declaration",
                "code": "INTERFACE I_Demo\nMETHOD Start : BOOL",
            })
        self.assertEqual("blocked", result["status"])
        self.assertIn("interface-member-inline", {
            item["rule"] for item in result["review"]["blocking_findings"]
        })
        call.assert_called_once()

    def test_missing_struct_write_returns_creation_hint_without_mutation(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(
            agent_core, "ps_com",
            side_effect=FileNotFoundError("Object 'ST_ProcParam' not found in any PLC folder."),
        ) as call:
            result = registry["plc_write"]["run"]({
                "name": "ST_ProcParam", "area": "declaration",
                "code": "TYPE ST_ProcParam : STRUCT\nEND_STRUCT\nEND_TYPE",
            })
        self.assertEqual("not_found", result["status"])
        self.assertFalse(result["written"])
        self.assertEqual("plc_create", result["recommended_tool"])
        self.assertEqual("struct", result["suggested_args"]["type"])
        call.assert_called_once()

    def test_protected_auto_allows_low_risk_but_asks_for_dangerous_tools(self) -> None:
        self.assertEqual("allow", agent_core.decide("auto", "plc_write"))
        self.assertEqual("allow", agent_core.decide("auto", "plc_lib_add"))
        self.assertEqual("ask", agent_core.decide("auto", "tc_run_mode"))
        self.assertEqual("ask", agent_core.decide("auto", "tc_target_set"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_delete_project"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_import_plcopen"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_export_plcopen"))
        self.assertEqual("ask", agent_core.decide("auto", "tc_io_export"))
        self.assertEqual("ask", agent_core.decide("auto", "plc_lib_install"))

    def test_accept_edits_does_not_silently_modify_libraries_or_runtime(self) -> None:
        self.assertEqual("allow", agent_core.decide("accept", "plc_write"))
        self.assertEqual("allow", agent_core.decide("accept", "plc_create_project"))
        self.assertEqual("ask", agent_core.decide("accept", "plc_lib_add"))
        self.assertEqual("ask", agent_core.decide("accept", "tc_config_mode"))

    def test_nc_tools_route_through_the_pid_bound_native_com_bridge(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "ok"}) as call:
            result = registry["nc_axis_info"]["run"]({"axis": "Axis 1"})
        self.assertEqual({"status": "ok"}, result)
        call.assert_called_once_with("nc-axis-info", axis="Axis 1")

    def test_target_and_io_mutations_require_confirmation(self) -> None:
        for name in ("tc_target_set", "tc_io_create", "tc_io_remove", "tc_scan_devices"):
            with self.subTest(name=name):
                self.assertEqual("ask", agent_core.decide("accept", name))
                self.assertEqual("deny", agent_core.decide("plan", name))
                self.assertEqual("ask", agent_core.decide("auto", name))

    def test_system_mutations_require_confirmation_but_previews_are_explicit(self) -> None:
        readonly = {"tc_system_structure", "tc_system_settings", "tc_core_info",
                    "tc_realtime_info", "tc_realtime_validate", "tc_task_info",
                    "tc_task_runtime_info"}
        mutations = {
            "tc_system_settings_set", "tc_realtime_settings_set", "tc_core_assign",
            "tc_task_core_assign", "tc_task_settings_set",
            "tc_system_add", "tc_system_remove",
        }
        for name in readonly:
            with self.subTest(name=name):
                self.assertTrue(agent_core.is_readonly(name))
                self.assertEqual("allow", agent_core.decide("plan", name))
        for name in mutations:
            with self.subTest(name=name):
                self.assertFalse(agent_core.is_readonly(name))
                self.assertEqual("system", agent_core.tool_metadata(name)["danger"])
                self.assertEqual("deny", agent_core.decide("plan", name))
                self.assertEqual("ask", agent_core.decide("accept", name))
                self.assertEqual("ask", agent_core.decide("auto", name))

    def test_safety_tools_keep_non_bypassable_hard_confirmation(self) -> None:
        readonly = {"tc_safety_structure", "tc_safety_project_info", "tc_safety_files",
                    "tc_safety_target_info", "tc_safety_aliases", "tc_safety_application",
                    "tc_safety_logic_check", "tc_safety_validate"}
        mutations = {"tc_safety_import", "tc_safety_create", "tc_safety_export", "tc_safety_remove",
                     "tc_safety_delete"}
        for name in readonly:
            with self.subTest(name=name):
                self.assertTrue(agent_core.is_readonly(name))
                self.assertEqual("allow", agent_core.decide("plan", name))
        for name in mutations:
            with self.subTest(name=name):
                self.assertFalse(agent_core.is_readonly(name))
                self.assertEqual("safety", agent_core.tool_metadata(name)["danger"])
                self.assertEqual("deny", agent_core.decide("plan", name))
                self.assertEqual("ask", agent_core.decide("accept", name))
                self.assertEqual("ask", agent_core.decide("auto", name))

    def test_safety_import_defaults_to_preview_contract(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "preview"}) as call:
            result = registry["tc_safety_import"]["run"]({
                "source": r"C:\templates\machine.tfzip", "name": "Safety1",
            })
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "safety-import", source=r"C:\templates\machine.tfzip", name="Safety1",
            mode="copy", apply=False, confirm_source_move=False,
            acknowledge_safety_review=False,
        )

    def test_safety_create_defaults_to_hardware_preview_contract(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "preview"}) as call:
            result = registry["tc_safety_create"]["run"]({"name": "Safety1"})
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "safety-create", name="Safety1", target="hardware",
            template="preconfigured-inputs",
            author="TwinCAT Agent", internal_project_name="", apply=False,
            acknowledge_safety_review=False,
        )

    def test_safety_logic_check_routes_as_read_only_contract(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "valid"}) as call:
            result = registry["tc_safety_logic_check"]["run"]({
                "project": r"C:\Safety\Machine.splcproj", "group": "MainGroup",
            })
        self.assertEqual("valid", result["status"])
        call.assert_called_once_with(
            "safety-logic-check", project=r"C:\Safety\Machine.splcproj", group="MainGroup",
        )

    def test_safety_delete_defaults_to_preview_contract(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        with patch.object(agent_core, "ps_com", return_value={"status": "preview"}) as call:
            result = registry["tc_safety_delete"]["run"]({"project": "Safety1"})
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "safety-delete", project="Safety1", backup_file="", apply=False,
            confirm_project_name="", confirm_delete_files=False,
            acknowledge_safety_review=False,
        )

    def test_safety_delete_is_default_and_accepts_orphan_selector(self) -> None:
        registry = {tool["name"]: tool for tool in agent_core.REGISTRY}
        delete_tool = registry["tc_safety_delete"]
        remove_tool = registry["tc_safety_remove"]
        self.assertIn("默认工具", delete_tool["description"])
        self.assertIn("孤立 .splcproj", delete_tool["description"])
        self.assertIn("明确要求保留", remove_tool["description"])
        selector_help = delete_tool["parameters"]["properties"]["project"]["description"]
        self.assertIn("项目目录", selector_help)

    def test_safety_orphan_delete_keeps_backup_and_solution_boundary_gates(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        start = script.index("function Delete-TcSafetyProject")
        end = script.index("function Close-TcSolution", start)
        delete = script[start:end]
        self.assertIn("filesystem_orphan", delete)
        self.assertIn("CreateFromDirectory", delete)
        self.assertIn("confirm_delete_files=true", delete)
        self.assertIn("acknowledge_safety_review=true", delete)
        self.assertIn("outside the current solution directory", delete)

    def test_io_manifest_can_be_checked_without_xae(self) -> None:
        manifest = {
            "schema_version": 1,
            "master": {"name": "EtherCAT Master", "subtype": 111},
            "devices": [{
                "id": "ek1100",
                "name": "EK1100",
                "parent": "$master",
                "subtype": 9099,
                "product_candidates": ["EK1100-0000-0018"],
            }],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "io.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = ps_io_configuration("check-manifest", manifest=str(path))
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(1, result["validation"]["device_count"])
        inline = ps_io_configuration(
            "check-manifest", configuration=manifest)
        self.assertTrue(inline["validation"]["ok"])

    def test_io_bridge_never_scans_activates_or_restarts(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "Invoke-TcIoConfiguration.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ActivateConfiguration(", script)
        self.assertNotIn("StartRestartTwinCAT(", script)
        self.assertNotIn("ScanDevices(", script)
        self.assertNotIn("StaticRoutes.xml", script)

    def test_hardware_scan_has_no_hidden_mode_or_runtime_changes(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tc_template"
            / "TcCom.ps1"
        ).read_text(encoding="utf-8-sig")
        start = script.index("function Invoke-TcIoScan")
        end = script.index(
            "# ---- 内部: 单遍新鲜遍历定位 POU", start)
        scan = script[start:end]
        self.assertIn("ProduceXml($false)", scan)
        self.assertIn("<ScanBoxes>1</ScanBoxes>", scan)
        self.assertNotIn("ActivateConfiguration", scan)
        self.assertNotIn("StartRestartTwinCAT", scan)
        self.assertNotIn("RestartTwinCATConfigMode", scan)
        self.assertNotIn("Set-TcRtState", scan)

    def test_ads_net_id_validation(self) -> None:
        self.assertEqual((1, 2, 3, 4, 5, 6), _parse_net_id("1.2.3.4.5.6"))
        for invalid in ("", "1.2.3", "1.2.3.4.5.999", "a.b.c.d.e.f"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AdsStateError):
                    _parse_net_id(invalid)

    def test_scan_accepts_ads_config_state_15(self) -> None:
        config_state = {
            "net_id": "1.2.3.4.1.1",
            "port": 300,
            "state_code": 15,
            "state_name": "Config/CP-Panel",
            "device_state": 0,
            "is_config": True,
        }
        with (
            patch.object(agent_core, "read_ads_state", return_value=config_state),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"found": [], "mode": "Config (ADS)"},
            ]
            result = agent_core._scan_devices({})
        self.assertEqual(15, result["ads_state"]["state_code"])
        self.assertEqual(
            ("io-scan",),
            call.call_args_list[1].args,
        )
        self.assertTrue(call.call_args_list[1].kwargs["config_confirmed"])

    def test_scan_attempts_safely_when_ads_probe_is_unavailable(self) -> None:
        with (
            patch.object(
                agent_core, "read_ads_state",
                side_effect=AdsStateError("ADS route unavailable")),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"found": [], "mode": "Unknown"},
            ]
            result = agent_core._scan_devices({})
        self.assertIn("ADS route unavailable", result["state_check_warning"])
        self.assertTrue(call.call_args_list[1].kwargs["allow_unknown"])

    def test_config_mode_accepts_already_config_state_15(self) -> None:
        state = {
            "state_code": 15,
            "state_name": "Config/CP-Panel",
            "is_config": True,
        }
        with (
            patch.object(agent_core, "read_ads_state", return_value=state),
            patch.object(
                agent_core, "ps_com",
                return_value={"target_netid": "1.2.3.4.1.1"}) as call,
        ):
            result = agent_core._set_config_mode({})
        self.assertEqual("already_config", result["status"])
        self.assertTrue(result["verified"])
        call.assert_called_once_with("target-show")

    def test_config_mode_uses_official_fallback_and_verifies(self) -> None:
        run = {"state_code": 5, "state_name": "Run", "is_config": False}
        config = {"state_code": 7, "state_name": "Config", "is_config": True}
        with (
            patch.object(agent_core, "read_ads_state", return_value=run),
            patch.object(
                agent_core, "_wait_ads_state",
                side_effect=[(run, ""), (run, ""), (config, "")]),
            patch.object(agent_core, "write_ads_control", return_value={}),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"requested": "Config", "strategy": "TIRS ConsumeXml"},
                {"requested": "Config",
                 "strategy": "TwinCAT.RestartTwinCATConfigMode"},
            ]
            result = agent_core._set_config_mode({})
        self.assertTrue(result["verified"])
        self.assertEqual(
            "TwinCAT.RestartTwinCATConfigMode", result["strategy"])
        self.assertEqual("command", call.call_args_list[2].kwargs["strategy"])

    def test_config_mode_prefers_ads_system_service_and_verifies(self) -> None:
        run = {"state_code": 5, "state_name": "Run", "is_config": False}
        config = {"state_code": 15, "state_name": "Config/CP-Panel", "is_config": True}
        with (
            patch.object(agent_core, "read_ads_state", return_value=run),
            patch.object(agent_core, "_wait_ads_state", return_value=(config, "")) as wait,
            patch.object(agent_core, "write_ads_control", return_value={"requested_state": 8}) as control,
            patch.object(agent_core, "ps_com", return_value={"target_netid": "1.2.3.4.1.1"}) as call,
        ):
            result = agent_core._set_config_mode({})
        self.assertTrue(result["verified"])
        self.assertEqual("ADS System Service RECONFIG", result["strategy"])
        control.assert_called_once_with("1.2.3.4.1.1", 10000, 8)
        wait.assert_called_once_with("1.2.3.4.1.1", {7, 8, 15}, 30.0, port=10000)
        call.assert_called_once_with("target-show")

    def test_config_mode_never_reports_unverified_success(self) -> None:
        run = {"state_code": 5, "state_name": "Run", "is_config": False}
        with (
            patch.object(agent_core, "read_ads_state", return_value=run),
            patch.object(
                agent_core, "_wait_ads_state",
                side_effect=[(run, ""), (run, ""), (run, "")]),
            patch.object(agent_core, "write_ads_control", return_value={}),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"requested": "Config", "strategy": "TIRS ConsumeXml"},
                {"requested": "Config",
                 "strategy": "TwinCAT.RestartTwinCATConfigMode"},
            ]
            result = agent_core._set_config_mode({})
        self.assertFalse(result["verified"])
        self.assertIn("error", result)

    def test_config_mode_falls_back_when_consume_raises(self) -> None:
        run = {"state_code": 5, "state_name": "Run", "is_config": False}
        config = {"state_code": 7, "state_name": "Config", "is_config": True}
        with (
            patch.object(agent_core, "read_ads_state", return_value=run),
            patch.object(agent_core, "_wait_ads_state",
                         side_effect=[(run, ""), (run, ""), (config, "")]),
            patch.object(agent_core, "write_ads_control", return_value={}),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                RuntimeError("ConsumeXml failed"),
                {"requested": "Config", "strategy": "command"},
            ]
            result = agent_core._set_config_mode({})
        self.assertTrue(result["verified"])
        self.assertIn("ConsumeXml failed", result["attempts"][1]["error"])
        self.assertEqual("command", call.call_args_list[2].kwargs["strategy"])

    def test_run_mode_requires_ads_verification(self) -> None:
        config = {"state_code": 7, "state_name": "Config", "is_config": True}
        run = {"state_code": 5, "state_name": "Run", "is_config": False}
        with (
            patch.object(agent_core, "read_ads_state", return_value=config),
            patch.object(agent_core, "_wait_ads_state", return_value=(run, "")),
            patch.object(agent_core, "write_ads_control", return_value={"requested_state": 2}),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"},
                {"requested": "Run"},
            ]
            result = agent_core._set_run_mode({})
        self.assertTrue(result["verified"])
        self.assertEqual("run", result["status"])
        call.assert_called_once_with("target-show")

    def test_runtime_command_delegates_selection_to_shared_bridge(self) -> None:
        with patch.object(agent_core, "ps_com", return_value={"verified": True}) as call:
            result = agent_core._runtime_command("start", {5}, args={'all_plcs': True})
        self.assertTrue(result["verified"])
        call.assert_called_once_with("start", timeout=20.0, all_plcs=True)

    def test_login_preserves_incomplete_shared_result(self) -> None:
        with patch.object(agent_core, "ps_com", return_value={"verified": False, "status": "incomplete"}):
            result = agent_core._runtime_command("login")
        self.assertFalse(result["verified"])
        self.assertEqual("incomplete", result["status"])

    def test_run_mode_never_reports_unverified_success(self) -> None:
        config = {"state_code": 7, "state_name": "Config", "is_config": True}
        with (
            patch.object(agent_core, "read_ads_state", return_value=config),
            patch.object(agent_core, "_wait_ads_state", return_value=(config, "")),
            patch.object(agent_core, "write_ads_control", return_value={"requested_state": 2}),
            patch.object(agent_core, "ps_com") as call,
        ):
            call.side_effect = [
                {"target_netid": "1.2.3.4.1.1"}, RuntimeError("Run failed")]
            result = agent_core._set_run_mode({})
        self.assertFalse(result["verified"])
        self.assertIn("error", result)
        self.assertIn("Run failed", result["attempts"][1]["error"])

    def test_config_powershell_uses_current_system_manager_without_activation(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "tc_template"
            / "TcCom.ps1"
        ).read_text(encoding="utf-8-sig")
        start = script.index("function Set-TcRtState")
        end = script.index("# ---- 输出纯 ASCII JSON", start)
        config = script[start:end]
        self.assertIn("Get-TcSystemManager $Dte", config)
        self.assertIn("Invoke-TcConfigRestartCommand $Dte", config)
        self.assertIn("TwinCAT.RestartTwinCATConfigMode", script)
        self.assertNotIn("ActivateConfiguration", config)
        self.assertNotIn("StartRestartTwinCAT", config)

    def test_deploy_guidance_does_not_require_config_mode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        backend = (root / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        deploy = (root / "tc_template" / "tc_platform.py").read_text(encoding="utf-8")
        self.assertIn("不要为了部署而切换 Config 模式", backend)
        self.assertIn("does not switch the target to Config mode", deploy)
        deploy_source = deploy[deploy.index("def deploy("):]
        self.assertNotIn("set_config_mode(", deploy_source)


if __name__ == "__main__":
    unittest.main()
