from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_template.hmi_live import _ads_error_details, check_hmi_ads_online, diagnose_hmi_bindings


class HmiLiveCheckTests(unittest.TestCase):
    def _project(self, directory: str) -> dict:
        root = Path(directory)
        server = root / "Server" / "TcHmiSrv"
        server.mkdir(parents=True)
        (server / "TcHmiSrv.Config.default.json").write_text(json.dumps({
            "SYMBOLS": {
                "ADS.PLC1.GVL_Hmi.bStart": {
                    "DOMAIN": "ADS", "DYNAMIC": True, "USEMAPPING": True,
                    "MAPPING": "PLC1::GVL_Hmi::bStart",
                },
                "ADS.PLC1.GVL_Hmi.stStatus": {
                    "DOMAIN": "ADS", "DYNAMIC": True, "USEMAPPING": True,
                    "MAPPING": "PLC1::GVL_Hmi::stStatus",
                },
            }
        }), encoding="utf-8")
        return {"project_file": str(root / "Demo.hmiproj"), "project_directory": str(root)}

    def test_reads_configured_dynamic_symbols_from_matching_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory)
            with patch("tc_template._ps_bridge.com_hmi_project_info", return_value=project), \
                 patch("tc_template._ps_bridge.com_hmi_ads_info", return_value={"runtimes": [{
                     "scope": "default", "name": "PLC1", "enabled": True,
                     "netid": "1.2.3.4.1.1", "port": 852,
                 }]}), \
                 patch("tc_template._ps_bridge.ps_com", return_value={
                     "target_netid": "1.2.3.4.1.1", "plcs": [{
                         "name": "PlcA", "instance": "PlcA Instance", "ads_port": 852,
                         "port_source": "xml:AdsPort",
                     }],
                 }), \
                 patch("tc_agent.ads.read_ads_state", return_value={"state_code": 5, "state_name": "Run"}), \
                 patch("tc_agent.dynamic_ads.read_symbol", side_effect=lambda n, p, s, depth: {
                     "value": s.endswith("bStart"), "symbol": {"name": s},
                 }) as read:
                result = check_hmi_ads_online(project="Demo", symbols=["GVL_Hmi.bStart"])

        self.assertTrue(result["verified"])
        self.assertEqual(1, result["read_count"])
        self.assertEqual("GVL_Hmi.bStart", result["symbols"][0]["plc_symbol"])
        read.assert_called_once_with("1.2.3.4.1.1", 852, "GVL_Hmi.bStart", depth=3)

    def test_endpoint_mismatch_blocks_ads_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(directory)
            with patch("tc_template._ps_bridge.com_hmi_project_info", return_value=project), \
                 patch("tc_template._ps_bridge.com_hmi_ads_info", return_value={"runtimes": [{
                     "scope": "default", "name": "PLC1", "enabled": True,
                     "netid": "127.0.0.1.1.1", "port": 851,
                 }]}), \
                 patch("tc_template._ps_bridge.ps_com", return_value={
                     "target_netid": "1.2.3.4.1.1", "plcs": [{"name": "PlcA", "ads_port": 851}],
                 }), \
                 patch("tc_agent.ads.read_ads_state") as state:
                result = check_hmi_ads_online(project="Demo")

        self.assertEqual("configuration-mismatch", result["status"])
        self.assertFalse(result["verified"])
        state.assert_not_called()

    def test_ads_error_code_is_stable_when_message_text_is_mangled(self) -> None:
        self.assertEqual(
            {"ads_error_code": 6, "ads_error_name": "ERR_TARGETPORTNOTFOUND"},
            _ads_error_details("��ȡ target ʧ�� (ADS 6)"),
        )

    def _diagnostic_project(self, directory: str, symbols: list[str]) -> dict:
        root = Path(directory)
        solution = root / "Demo.sln"
        solution.write_text("", encoding="utf-8")
        tmc = root / "System" / "PLC1" / "PLC1.tmc"
        tmc.parent.mkdir(parents=True)
        body = "".join(
            f"<Symbol><Name>{name}</Name><BaseType>BOOL</BaseType></Symbol>"
            for name in symbols
        )
        tmc.write_text(
            f"<TcModuleClass><Modules><Module><DataAreas><DataArea>{body}</DataArea>"
            "</DataAreas></Module></Modules></TcModuleClass>", encoding="utf-8",
        )
        (root / "System" / "Demo.tsproj").write_text(
            '<Project><Plc><Project Name="PLC1" TmcFilePath="PLC1\\PLC1.tmc" AmsPort="851" /></Plc></Project>',
            encoding="utf-8",
        )
        return {
            "project_name": "HMI1", "project_file": str(root / "HMI1.hmiproj"),
            "project_directory": str(root), "solution": str(solution),
        }

    def test_binding_diagnosis_separates_missing_mapping_from_offline_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._diagnostic_project(
                directory, ["GVL_Hmi.bStart", "GVL_Hmi.sRecipeName"],
            )
            binding_result = {
                "project": "HMI1", "binding_count": 2, "valid": True,
                "error_count": 0, "warning_count": 1, "findings": [{
                    "severity": "warning", "code": "binding-unmapped",
                    "expression": "%s%ADS.PLC1.GVL_Hmi.sRecipeName%/s%",
                }],
                "bindings": [
                    {"kind": "server", "path": "ADS.PLC1.GVL_Hmi.bStart", "state": "mapped"},
                    {"kind": "server", "path": "ADS.PLC1.GVL_Hmi.sRecipeName", "state": "unmapped"},
                ],
            }
            endpoint = {"runtimes": [{"scope": "default", "name": "PLC1", "enabled": True,
                                        "netid": "1.2.3.4.1.1", "port": 851}]}
            inventory = {"target_netid": "1.2.3.4.1.1", "plcs": [{
                "name": "PLC1", "instance": "PLC1 Instance", "ads_port": 851,
                "port_source": "xml:AdsPort",
            }]}
            with patch("tc_template._ps_bridge.com_hmi_project_info", return_value=project), \
                 patch("tc_template._ps_bridge.com_hmi_bindings", return_value=binding_result), \
                 patch("tc_template._ps_bridge.com_hmi_ads_info", return_value=endpoint), \
                 patch("tc_template._ps_bridge.ps_com", return_value=inventory), \
                 patch("tc_template.hmi_live.check_hmi_ads_online", return_value={
                     "status": "offline", "verified": False,
                     "error": "读取 1.2.3.4.1.1:851 状态失败（ADS 6）",
                 }):
                result = diagnose_hmi_bindings(project="HMI1")

        codes = {item["code"] for item in result["root_causes"]}
        self.assertEqual("blocked", result["status"])
        self.assertIn("hmi-mapping-missing", codes)
        self.assertIn("plc-runtime-unreachable", codes)
        self.assertEqual("server-mapping", result["blocking_stage"])
        bind = next(item for item in result["recommended_sequence"]
                    if item["tool"] == "tc_hmi_bind_plc")
        self.assertEqual(["GVL_Hmi"], bind["args"]["symbol_roots"])
        self.assertFalse(bind["args"]["apply"])

    def test_binding_diagnosis_does_not_offer_mapping_for_symbol_absent_from_tmc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._diagnostic_project(directory, ["GVL_Hmi.bStart"])
            bindings = {
                "project": "HMI1", "binding_count": 1, "valid": True,
                "error_count": 0, "warning_count": 1, "findings": [],
                "bindings": [{"kind": "server", "path": "ADS.PLC1.GVL_Hmi.missing", "state": "unmapped"}],
            }
            with patch("tc_template._ps_bridge.com_hmi_project_info", return_value=project), \
                 patch("tc_template._ps_bridge.com_hmi_bindings", return_value=bindings), \
                 patch("tc_template._ps_bridge.com_hmi_ads_info", return_value={"runtimes": [{
                     "scope": "default", "name": "PLC1", "enabled": True,
                     "netid": "1.2.3.4.1.1", "port": 851,
                 }]}), \
                 patch("tc_template._ps_bridge.ps_com", return_value={
                     "target_netid": "1.2.3.4.1.1", "plcs": [{"name": "PLC1", "ads_port": 851}],
                 }):
                result = diagnose_hmi_bindings(project="HMI1")

        self.assertIn("tmc-symbol-missing", {item["code"] for item in result["root_causes"]})
        self.assertNotIn("tc_hmi_bind_plc", [item["tool"] for item in result["recommended_sequence"]])

    def test_binding_diagnosis_is_ready_only_after_static_and_online_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._diagnostic_project(directory, ["GVL_Hmi.bStart"])
            bindings = {
                "project": "HMI1", "binding_count": 1, "valid": True,
                "error_count": 0, "warning_count": 0, "findings": [],
                "bindings": [{"kind": "server", "path": "ADS.PLC1.GVL_Hmi.bStart", "state": "mapped"}],
            }
            with patch("tc_template._ps_bridge.com_hmi_project_info", return_value=project), \
                 patch("tc_template._ps_bridge.com_hmi_bindings", return_value=bindings), \
                 patch("tc_template._ps_bridge.com_hmi_ads_info", return_value={"runtimes": [{
                     "scope": "default", "name": "PLC1", "enabled": True,
                     "netid": "1.2.3.4.1.1", "port": 851,
                 }]}), \
                 patch("tc_template._ps_bridge.ps_com", return_value={
                     "target_netid": "1.2.3.4.1.1", "plcs": [{"name": "PLC1", "ads_port": 851}],
                 }), \
                 patch("tc_template.hmi_live.check_hmi_ads_online", return_value={
                     "status": "verified", "verified": True, "read_count": 1, "failed_count": 0,
                 }):
                result = diagnose_hmi_bindings(project="HMI1")

        self.assertEqual("ready", result["status"])
        self.assertTrue(result["verified"])
        self.assertEqual([], result["root_causes"])


def test_member_resolution_uses_registered_mapping_and_rejects_prefix_collision():
    from tc_template.hmi_live import _resolve_mapped_member
    root = {'server_symbol': 'ADS.PLC1.Values', 'plc_symbol': 'GVL.aValues',
            'mapping': 'PLC1::GVL::aValues', 'schema_type': 'Array'}
    available = {root['server_symbol']: root}
    resolved = _resolve_mapped_member('ADS.PLC1.Values[2].bReady', available)
    assert resolved['plc_symbol'] == 'GVL.aValues[2].bReady'
    assert resolved['mapped_root'] == root['server_symbol']
    assert resolved['schema_type'] is None  # root type is not the member type
    assert _resolve_mapped_member('ADS.PLC1.ValuesOther[2]', available) is None
    assert _resolve_mapped_member('ADS.PLC1.Values[garbage]', available) is None


def test_browser_stale_reference_does_not_recommend_remapping():
    from tc_template.hmi_live import explain_browser_binding_mismatch
    result = explain_browser_binding_mismatch({'status': 'failed', 'viewport_results': [
        {'diagnostics': [{'message': '[Control=BtnStart, Property=StateSymbol, Symbol=%s%OLD%/s%]'}]}
    ]}, {'bindings': [{'control': 'BtnStart', 'expression': '%s%NEW%/s%'}]})
    assert result['blocking_stage'] == 'hmi-runtime-content'
    assert result['recommended_sequence'][0]['tool'] == 'tc_hmi_build'


def test_binding_tool_profile_includes_page_edit_contracts():
    from tc_agent.backend import _auto_tool_names
    names = _auto_tool_names('重新绑定HMI和PLC变量', {'HMI'})
    assert {'tc_hmi_bind_plc', 'tc_hmi_control_schema', 'tc_hmi_controls_batch',
            'tc_hmi_control_edit', 'tc_hmi_control_events'} <= names


if __name__ == "__main__":
    unittest.main()
