from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tc_template.hmi_binding import _find_tmc, list_tmc_symbols, parse_tmc_bindings


def test_binding_transaction_reports_exact_failed_stage_and_rollback():
    source = (Path(__file__).parents[1] / 'tc_template/TcHmiBinding.ps1').read_text(encoding='utf-8-sig')
    assert "$stage = 'file-write'" in source
    assert "$stage = 'xae-reload'" in source
    assert "$stage = 'readback-runtime'" in source
    assert "$stage = 'readback-symbols'" in source
    assert "status='failed'; success=$false; verified=$false; stage=$stage" in source
    assert "files_restored=$filesRestored; project_reloaded=$projectReloaded" in source
    assert "Never report the binding as applied" in source


TMC = """<?xml version="1.0" encoding="utf-8"?>
<TcModuleClass>
  <DataTypes>
    <DataType><Name>E_State</Name><BaseType>INT</BaseType>
      <EnumInfo><Text>Idle</Text><Enum>0</Enum></EnumInfo>
      <EnumInfo><Text>Run</Text><Enum>10</Enum></EnumInfo>
    </DataType>
    <DataType><Name>ST_Status</Name>
      <SubItem><Name>eState</Name><Type>E_State</Type><Comment>State</Comment></SubItem>
      <SubItem><Name>sText</Name><Type>STRING(32)</Type></SubItem>
      <SubItem><Name>nValues</Name><Type>UINT</Type><ArrayInfo><LBound>0</LBound><Elements>4</Elements></ArrayInfo></SubItem>
    </DataType>
  </DataTypes>
  <Modules><Module><DataAreas><DataArea>
    <Symbol><Name>GVL_Hmi.bStart</Name><BaseType>BOOL</BaseType><Comment>Start</Comment></Symbol>
    <Symbol><Name>GVL_Hmi.stStatus</Name><BaseType>ST_Status</BaseType></Symbol>
    <Symbol><Name>GVL_Other.nValue</Name><BaseType>DINT</BaseType></Symbol>
  </DataArea></DataAreas></Module></Modules>
</TcModuleClass>
"""


class HmiBindingTests(unittest.TestCase):
    def test_tmc_generates_dynamic_symbols_and_recursive_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Demo.tmc"
            path.write_text(TMC, encoding="utf-8")
            result = parse_tmc_bindings(
                path, "PLC2", ["GVL_Hmi"], ["GVL_Hmi.stStatus"],
            )

        self.assertEqual(2, len(result["symbols"]))
        start = result["symbols"]["ADS.PLC2.GVL_Hmi.bStart"]
        status = result["symbols"]["ADS.PLC2.GVL_Hmi.stStatus"]
        self.assertEqual("PLC2::GVL_Hmi::bStart", start["MAPPING"])
        self.assertEqual(3, start["ACCESS"])
        self.assertEqual(1, status["ACCESS"])
        self.assertIn("ADS-PLC2.ST_Status", result["definitions"])
        self.assertIn("ADS-PLC2.E_State", result["definitions"])
        self.assertIn("ADS-PLC2.STRING-32", result["definitions"])
        values = result["definitions"]["ADS-PLC2.ST_Status"]["properties"]["nValues"]
        array_schema = values["allOf"][0]
        self.assertEqual(4, array_schema["minItems"])
        self.assertEqual(4, array_schema["maxItems"])

    def test_no_matching_symbol_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Demo.tmc"
            path.write_text(TMC, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "No TMC symbols matched"):
                parse_tmc_bindings(path, "PLC1", ["GVL_Missing"])

    def test_find_tmc_uses_tsproj_metadata_instead_of_port_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solution = root / "Demo.sln"
            solution.write_text("", encoding="utf-8")
            tmc = root / "System" / "PlcA" / "Actual.tmc"
            tmc.parent.mkdir(parents=True)
            tmc.write_text(TMC, encoding="utf-8")
            (root / "System" / "Demo.tsproj").write_text(
                '<Project><Plc><Project Name="PlcA" TmcFilePath="PlcA\\Actual.tmc" AmsPort="852" /></Plc></Project>',
                encoding="utf-8",
            )
            self.assertEqual(tmc.resolve(), _find_tmc(solution, "PlcA"))

    def test_tmc_inventory_only_reports_exported_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Demo.tmc"
            path.write_text(TMC, encoding="utf-8")
            self.assertEqual(
                ["GVL_Hmi.bStart", "GVL_Hmi.stStatus", "GVL_Other.nValue"],
                list_tmc_symbols(path),
            )


def test_saved_mapping_reuse_checks_endpoint_definitions_and_preserves_extra_symbols(tmp_path):
    import json
    from tc_template.hmi_binding import _saved_binding_matches
    server = tmp_path / 'Server'
    (server / 'ADS').mkdir(parents=True)
    (server / 'TcHmiSrv').mkdir()
    ads = server / 'ADS' / 'ADS.Config.default.json'
    ads.write_text(json.dumps({'RUNTIMES': {'PLC1': {'NETID': '1.2.3.4.1.1', 'PORT': 852}}}))
    config = server / 'TcHmiSrv' / 'TcHmiSrv.Config.default.json'
    config.write_text(json.dumps({'SYMBOLS': {'S': {'ACCESS': 3}, 'Other': {}},
                                 'DEFINITIONS': {'ADS': {'D': {'type': 'boolean'}}}}))
    generated = {'symbols': {'S': {'ACCESS': 3}}, 'definitions': {'D': {'type': 'boolean'}}}
    args = (tmp_path / 'Demo.hmiproj', 'PLC1', '1.2.3.4.1.1', 852, 'default', generated)
    assert _saved_binding_matches(*args)
    generated['definitions']['D'] = {'type': 'integer'}
    assert not _saved_binding_matches(*args)
    generated['definitions']['D'] = {'type': 'boolean'}
    ads.write_text(json.dumps({'RUNTIMES': {'PLC1': {'NETID': '1.2.3.4.1.1', 'PORT': 851}}}))
    assert not _saved_binding_matches(*args)


if __name__ == "__main__":
    unittest.main()
