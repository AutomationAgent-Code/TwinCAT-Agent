import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_template import _ps_bridge as bridge


class PowerShellFallbackTests(unittest.TestCase):
    def test_packaged_same_bitness_runtime_still_uses_process_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module = root / "app" / "tc_template" / "_ps_bridge.py"
            module.parent.mkdir(parents=True)
            helper = root / "runtime" / "python" / "python.exe"
            helper.parent.mkdir(parents=True)
            helper.touch()
            with patch.object(bridge, "__file__", str(module)), \
                 patch.object(bridge, "_process_is_64bit", return_value=True), \
                 patch.object(bridge.struct, "calcsize", return_value=8), \
                 patch.dict(os.environ, {"TC_AGENT_COM_HELPER": ""}):
                self.assertTrue(os.path.samefile(helper, bridge._native_helper(4024)))

    def test_auto_backend_skips_native_when_pywin32_is_unavailable(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"connected":true}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", return_value=None), \
             patch("tc_template._native_bridge.available", return_value=(False, "missing")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge.ps_com("connect-check")
        self.assertTrue(result["connected"])
        self.assertIn("powershell", run.call_args.args[0][0].lower())

    def test_auto_backend_routes_system_commands_to_powershell(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"status":"ok"}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", side_effect=AssertionError("native must not run")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge.ps_com("system-settings")
        self.assertEqual({"status": "ok"}, result)
        self.assertIn("powershell", run.call_args.args[0][0].lower())

    def test_auto_backend_routes_realtime_refresh_to_powershell(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"status":"refreshed"}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", side_effect=AssertionError("native must not run")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge.com_realtime_refresh()
        self.assertEqual({"status": "refreshed"}, result)
        self.assertIn("powershell", run.call_args.args[0][0].lower())

    def test_auto_backend_routes_safety_logic_check_to_powershell(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"status":"valid"}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", side_effect=AssertionError("native must not run")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge.com_safety_logic_check(r"C:\Safety\Machine.splcproj")
        self.assertEqual({"status": "valid"}, result)
        self.assertIn("powershell", run.call_args.args[0][0].lower())

    def test_auto_backend_routes_hmi_tools_to_powershell(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"status":"valid"}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", side_effect=AssertionError("native must not run")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge._ps_com_raw("hmi-validate", project="TcHmiProject1")
        self.assertEqual({"status": "valid"}, result)
        self.assertIn("powershell", run.call_args.args[0][0].lower())

    def test_hmi_localization_wrapper_preserves_locale_values(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_localization_set(
                "StartButton", values={"en": "Start", "de": "Starten"},
            )
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-localization-set", project="", key="StartButton", action="upsert",
            values={"en": "Start", "de": "Starten"}, apply=False,
        )

    def test_hmi_themed_resource_wrapper_rejects_invalid_action(self):
        with self.assertRaisesRegex(ValueError, "upsert or remove"):
            bridge.com_hmi_themed_resource_set("Accent", action="replace")

    def test_hmi_user_control_create_wrapper_preserves_nested_data(self):
        controls = [{
            "id": "Title", "type": "TcHmi.Controls.Beckhoff.TcHmiTextblock",
            "attributes": {"data-tchmi-text": "Probe"},
        }]
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_user_control_create(
                "MachineCard", project="Hmi", controls=controls,
            )
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-user-control-create", project="Hmi", name="MachineCard",
            parameters=[], controls=controls, apply=False,
        )

    def test_hmi_user_control_parameter_wrapper_rejects_invalid_action(self):
        with self.assertRaisesRegex(ValueError, "upsert or remove"):
            bridge.com_hmi_user_control_parameter_set(
                "MachineCard", "data-tchmi-title", action="replace",
            )

    def test_hmi_framework_create_wrapper_preserves_contract(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_framework_create(
                "MachineControls", r"C:\Hmi", project="Hmi",
                language="typescript", description="Reusable controls",
            )
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-framework-create", project="Hmi", name="MachineControls",
            output_directory=r"C:\Hmi", language="typescript",
            description="Reusable controls", apply=False, timeout=120.0,
        )

    def test_hmi_framework_create_wrapper_rejects_invalid_language(self):
        with self.assertRaisesRegex(ValueError, "typescript or javascript"):
            bridge.com_hmi_framework_create("MachineControls", r"C:\Hmi", language="python")

    def test_hmi_framework_attribute_wrapper_preserves_contract(self):
        settings = {"value_kind": "boolean", "default_value": False}
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_framework_attribute_set(
                r"C:\Hmi\Controls", "IsActive", control="MachineControl",
                settings=settings,
            )
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-framework-attribute-set", source=r"C:\Hmi\Controls",
            control="MachineControl", name="IsActive", action="upsert",
            settings=settings, apply=False,
        )

    def test_hmi_framework_event_wrapper_rejects_invalid_action(self):
        with self.assertRaisesRegex(ValueError, "upsert or remove"):
            bridge.com_hmi_framework_event_set(
                r"C:\Hmi\Controls", "ValueChanged", action="replace",
            )

    def test_hmi_framework_pack_wrapper_does_not_install(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_framework_pack(
                r"C:\Hmi\Controls", output_directory=r"C:\Packages", version="1.2.3",
            )
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-framework-pack", source=r"C:\Hmi\Controls",
            output_directory=r"C:\Packages", version="1.2.3",
            apply=False, timeout=120.0,
        )

    def test_hmi_framework_install_wrapper_requires_explicit_ack_flag(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_framework_install(r"C:\Packages\Machine.1.0.0.nupkg", project="Hmi")
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-framework-install", package=r"C:\Packages\Machine.1.0.0.nupkg",
            project="Hmi", apply=False, acknowledge_package_change=False, timeout=180.0,
        )

    def test_hmi_framework_uninstall_wrapper_preserves_reference_gate_flags(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_framework_uninstall("Machine.Controls", project="Hmi", force=True)
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-framework-uninstall", package_id="Machine.Controls", project="Hmi",
            force=True, apply=False, acknowledge_package_change=False, timeout=180.0,
        )

    def test_hmi_browser_validate_wrapper_preserves_viewports(self):
        with patch.object(bridge, "ps_com", return_value={"status": "passed"}) as call:
            result = bridge.com_hmi_browser_validate(
                project="Hmi", widths=[375, 1280], height=800, settle_ms=2500,
            )
        self.assertEqual("passed", result["status"])
        call.assert_called_once_with(
            "hmi-browser-validate", project="Hmi", widths=[375, 1280],
            height=800, settle_ms=2500, timeout=180.0,
        )

    def test_hmi_browser_validate_explicit_view_uses_target_loader(self):
        with patch('tc_template.hmi_browser_target.validate_entry_page',
                   return_value={"status": "passed", "loaded_view": "Main.view"}) as load:
            result = bridge.com_hmi_browser_validate(
                project="Hmi", entry_page="Main.view", widths=[1280], settle_ms=1500,
            )
        self.assertEqual("Main.view", result["loaded_view"])
        load.assert_called_once_with("Hmi", "Main.view", [1280], 720, 1500)

    def test_hmi_server_control_wrapper_requires_apply_for_mutation(self):
        with patch.object(bridge, "ps_com", return_value={"status": "preview"}) as call:
            result = bridge.com_hmi_server_control("restart", project="Hmi")
        self.assertEqual("preview", result["status"])
        call.assert_called_once_with(
            "hmi-server-control", project="Hmi", action="restart",
            apply=False, timeout=60.0,
        )

    def test_symbol_expression_uses_json_file_transport(self):
        completed = type("Completed", (), {
            "stdout": '{"ok":true,"data":{"status":"preview"}}\n',
            "stderr": "",
            "returncode": 0,
        })()
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "auto"}), \
             patch.object(bridge, "_native_helper", side_effect=AssertionError("native must not run")), \
             patch.object(bridge.subprocess, "run", return_value=completed) as run:
            # This test isolates transport; write-contract enforcement is
            # covered separately with installed-schema fixtures.
            result = bridge._ps_com_raw(
                "hmi-control-edit", file="Desktop.view", action="add", control_id="Probe",
                type="TcHmi.Controls.Beckhoff.TcHmiButton",
                attributes={"data-tchmi-state-symbol": "%s%ADS.PLC1.MAIN.bRun%/s%"},
            )
        self.assertEqual("preview", result["status"])
        argv = run.call_args.args[0]
        self.assertIn("-ArgsFile", argv)
        self.assertNotIn("-ArgsJson", argv)

    def test_explicit_native_backend_does_not_fallback(self):
        with patch.dict(os.environ, {"TC_AGENT_COM_BACKEND": "native"}), \
             patch.object(bridge, "_native_request", side_effect=RuntimeError("missing")), \
             patch.object(bridge.subprocess, "run") as run:
            with self.assertRaises(bridge.TcComError):
                bridge.ps_com("connect-check")
        run.assert_not_called()

    def test_write_review_blocks_before_com_mutation(self):
        current = {
            "name": "FB_Review", "declaration": "FUNCTION_BLOCK FB_Review\nVAR_INPUT\nEND_VAR",
            "implementation": "", "methods": [],
        }
        with patch.object(bridge, "ps_com", return_value=current) as call:
            with self.assertRaises(bridge.PlcReviewError):
                bridge.com_write_pou(
                    "FB_Review", "FUNCTION_BLOCK FB_Review\nVAR_INPUT\n    nSetpoint : INT;\nEND_VAR\nEND_FUNCTION_BLOCK",
                    area="declaration",
                )
        call.assert_called_once_with(
            "read-pou", name="FB_Review", area="all", method="",
            include_member_code=False, start_line=1, max_lines=0, path="",
        )

    def test_write_review_allows_then_calls_com_mutation(self):
        current = {
            "name": "PRG_Main", "declaration": "PROGRAM PRG_Main\nVAR\n    nCount : UINT; // 计数\nEND_VAR",
            "implementation": "", "methods": [],
        }
        with patch.object(bridge, "ps_com") as call:
            call.side_effect = [current, {"status": "written"}]
            result = bridge.com_write_pou("PRG_Main", "nCount := nCount + 1;")
        self.assertEqual({"status": "written"}, result)
        self.assertEqual("write-pou", call.call_args_list[1].args[0])

    def test_method_write_blocks_missing_method_header(self):
        current = {
            "name": "FB_GcodeHand.ParseSpecialCmd",
            "declaration": "VAR_IN_OUT\n    stLine : ST_Gcode;\nEND_VAR",
            "implementation": "ParseSpecialCmd := TRUE;",
            "methods": [],
        }
        with patch.object(bridge, "ps_com", return_value=current):
            with self.assertRaisesRegex(bridge.PlcReviewError, "method-declaration-header"):
                bridge.com_write_pou(
                    "FB_GcodeHand", "ParseSpecialCmd := TRUE;",
                    method_name="ParseSpecialCmd",
                )

    def test_method_write_blocks_result_without_return_type(self):
        current = {
            "name": "FB_Test.Run",
            "declaration": "METHOD Run\nVAR\n    bLocal : BOOL;\nEND_VAR",
            "implementation": "Run := TRUE;",
            "methods": [],
        }
        with patch.object(bridge, "ps_com", return_value=current):
            with self.assertRaisesRegex(bridge.PlcReviewError, "method-return-type"):
                bridge.com_write_pou("FB_Test", "Run := TRUE;", method_name="Run")


if __name__ == "__main__":
    unittest.main()
