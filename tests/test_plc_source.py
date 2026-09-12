from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tc_agent.plc_source import read_source, source_index


class PlcSourceTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        solution = root / "Machine.sln"
        solution.write_text('Project("{fixture}") = "PLC1", "Machine/PLC1.plcproj", "{id}"\n', encoding="utf-8")
        project = root / "Machine" / "PLC1.plcproj"
        source = project.parent / "POUs" / "Deep" / "FB_Test.TcPOU"
        source.parent.mkdir(parents=True)
        project.write_text("""<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup><Name>PLC1</Name></PropertyGroup>
  <ItemGroup><Compile Include="POUs\\Deep\\FB_Test.TcPOU" /></ItemGroup>
</Project>""", encoding="utf-8")
        source.write_text("""<?xml version="1.0" encoding="utf-8"?>
<TcPlcObject><POU Name="FB_Test">
  <Declaration><![CDATA[FUNCTION_BLOCK FB_Test]]></Declaration>
  <Implementation><ST><![CDATA[nValue := 1;]]></ST></Implementation>
  <Method Name="Start"><Declaration><![CDATA[METHOD Start : BOOL]]></Declaration>
    <Implementation><ST><![CDATA[Start := TRUE;]]></ST></Implementation></Method>
  <Property Name="Value"><Declaration><![CDATA[PROPERTY Value : INT]]></Declaration>
    <Get><Declaration><![CDATA[GET]]></Declaration><Implementation><ST><![CDATA[Value := nValue;]]></ST></Implementation></Get>
  </Property>
</POU></TcPlcObject>""", encoding="utf-8")
        return solution

    def test_indexes_deep_compile_include_and_reads_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = self._project(Path(temp))
            entries = source_index(solution)
            self.assertEqual(1, len(entries))
            self.assertEqual("PLC1^POUs^Deep^FB_Test.TcPOU", entries[0].logical_path)
            result = read_source(solution, "FB_Test")
            self.assertEqual("disk", result["source"])
            self.assertEqual("saved_source", result["path_kind"])
            self.assertEqual(result["path"], result["source_path"])
            self.assertFalse(result["tree_path_available"])
            self.assertEqual("FUNCTION_BLOCK FB_Test", result["declaration"])
            self.assertEqual("nValue := 1;", result["implementation"])
            self.assertEqual(64, len(result["hashes"]["implementation"]))

    def test_reads_method_and_property_accessor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = self._project(Path(temp))
            method = read_source(solution, "FB_Test", member="Start")
            getter = read_source(solution, "FB_Test", member="Value.Get")
            self.assertEqual("Start := TRUE;", method["implementation"])
            self.assertEqual("Value := nValue;", getter["implementation"])

    def test_decodes_percent_encoded_legacy_folder_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            solution = root / "Machine.sln"
            solution.write_text('Project("{fixture}") = "PLC", "PLC/PLC.plcproj", "{id}"\n', encoding="utf-8")
            project = root / "PLC" / "PLC.plcproj"
            source = project.parent / "POUs" / "Station (123)" / "MAIN.TcPOU"
            source.parent.mkdir(parents=True)
            project.write_text("""<Project><PropertyGroup><Name>PLC</Name></PropertyGroup>
<ItemGroup><Compile Include="POUs\\Station %28123%29\\MAIN.TcPOU" /></ItemGroup></Project>""",
                               encoding="utf-8")
            source.write_text("""<TcPlcObject><POU Name="MAIN"><Declaration>PROGRAM MAIN</Declaration>
<Implementation><ST>n := 1;</ST></Implementation></POU></TcPlcObject>""", encoding="utf-8")
            result = read_source(solution, "MAIN")
            self.assertIn("Station (123)", result["file"])
            self.assertEqual("n := 1;", result["implementation"])

    def test_com_tree_path_is_not_fuzzy_matched_to_saved_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = self._project(Path(temp))
            with self.assertRaisesRegex(ValueError, "not a saved-source index path"):
                read_source(solution, "FB_Test", path="TIPC^PLC1^PLC1 Project^POUs^Deep^FB_Test")


if __name__ == "__main__":
    unittest.main()
