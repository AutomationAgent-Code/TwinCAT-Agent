import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tc_template import _native_bridge, plc


class DutDeclarationTests(unittest.TestCase):
    def test_struct_shorthand_is_wrapped_as_complete_type(self) -> None:
        result = plc.normalize_dut_declaration(
            "struct", "ST_MotorParams", "STRUCT\n    fVelocity : LREAL;\nEND_STRUCT"
        )
        self.assertEqual(
            "TYPE ST_MotorParams :\nSTRUCT\n    fVelocity : LREAL;\nEND_STRUCT\nEND_TYPE",
            result,
        )

    def test_enum_shorthand_gets_required_semicolon(self) -> None:
        result = plc.normalize_dut_declaration(
            "enum", "E_MotorState", "(\n    eIdle := 0,\n    eRun := 1\n)"
        )
        self.assertEqual(
            "TYPE E_MotorState :\n(\n    eIdle := 0,\n    eRun := 1\n);\nEND_TYPE",
            result,
        )

    def test_enum_preserves_attributes_and_explicit_base_type(self) -> None:
        declaration = """{attribute 'qualified_only'}
{attribute 'strict'}
TYPE E_State :
(
    Idle := 0,
    Running
) DINT;
END_TYPE"""
        result = plc.normalize_dut_declaration("enum", "E_State", declaration)
        self.assertTrue(result.startswith("{attribute 'qualified_only'}"))
        self.assertIn("{attribute 'strict'}\nTYPE E_State :", result)
        self.assertIn(") DINT;\nEND_TYPE", result)

    def test_explicit_dut_name_must_match_object_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            plc.normalize_dut_declaration(
                "struct", "ST_Expected",
                "TYPE ST_Wrong : STRUCT\nEND_STRUCT\nEND_TYPE",
            )

    def test_union_and_alias_shorthand_are_wrapped(self) -> None:
        union = plc.normalize_dut_declaration(
            "union", "U_Value", "UNION\n    nValue : DINT;\nEND_UNION"
        )
        alias = plc.normalize_dut_declaration("alias", "T_Count", "UDINT")
        self.assertEqual(
            "TYPE U_Value :\nUNION\n    nValue : DINT;\nEND_UNION\nEND_TYPE",
            union,
        )
        self.assertEqual("TYPE T_Count :\nUDINT;\nEND_TYPE", alias)

    def test_create_pou_passes_canonical_declaration_and_verifies_item_type(self) -> None:
        created = SimpleNamespace(Name="ST_Data", ItemType=606)

        class Folder:
            def __init__(self):
                self.calls = []

            def CreateChild(self, name, subtype, before, info):
                self.calls.append((name, subtype, before, info))

        folder = Folder()
        sysman = SimpleNamespace(
            LookupTreeItem=lambda path: created,
            Solution=SimpleNamespace(),
        )
        dte = SimpleNamespace(
            MainWindow=SimpleNamespace(Visible=False),
            Solution=SimpleNamespace(
                Projects=SimpleNamespace(Item=lambda index: SimpleNamespace(Object=sysman))
            ),
        )
        with patch.object(plc, "_com_dte", return_value=dte), \
             patch.object(plc, "_find_nested_project", return_value=object()), \
             patch.object(plc, "_get_nested_base_path", return_value="TIPC^PLC^PLC Project"), \
             patch.object(plc, "_find_object_in_folder", return_value=folder), \
             patch.object(plc._time, "sleep"):
            result = plc.create_pou(
                "struct", "ST_Data",
                declaration="STRUCT\n    nValue : INT;\nEND_STRUCT",
            )
        self.assertEqual("created", result["status"])
        self.assertEqual(606, folder.calls[0][1])
        self.assertTrue(folder.calls[0][3].startswith("TYPE ST_Data :\nSTRUCT"))

    def test_create_pou_rolls_back_wrong_xae_item_type(self) -> None:
        created = SimpleNamespace(Name="ST_Data", ItemType=623)

        class Folder:
            def __init__(self):
                self.deleted = []

            def CreateChild(self, *_args):
                pass

            def DeleteChild(self, name):
                self.deleted.append(name)

        folder = Folder()
        sysman = SimpleNamespace(LookupTreeItem=lambda path: created)
        dte = SimpleNamespace(
            MainWindow=SimpleNamespace(Visible=False),
            Solution=SimpleNamespace(
                Projects=SimpleNamespace(Item=lambda index: SimpleNamespace(Object=sysman))
            ),
        )
        with patch.object(plc, "_com_dte", return_value=dte), \
             patch.object(plc, "_find_nested_project", return_value=object()), \
             patch.object(plc, "_get_nested_base_path", return_value="TIPC^PLC^PLC Project"), \
             patch.object(plc, "_find_object_in_folder", return_value=folder), \
             patch.object(plc._time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "expected 606"):
                plc.create_pou(
                    "struct", "ST_Data",
                    declaration="STRUCT\n    nValue : INT;\nEND_STRUCT",
                )
        self.assertEqual(["ST_Data"], folder.deleted)

    def test_write_rejects_attempt_to_turn_existing_alias_into_struct(self) -> None:
        alias = SimpleNamespace(Name="ST_Broken", ItemType=623, DeclarationText="INT")
        with patch.object(_native_bridge, "_system_manager", return_value=object()), \
             patch.object(
                 _native_bridge, "_find_object",
                 return_value=(alias, "TIPC^PLC^DUTs^ST_Broken"),
             ):
            with self.assertRaisesRegex(ValueError, "delete and recreate"):
                _native_bridge._write_pou(object(), {
                    "name": "ST_Broken", "area": "declaration",
                    "code": "STRUCT\n    nValue : INT;\nEND_STRUCT",
                })

    def test_write_normalizes_existing_struct_declaration(self) -> None:
        struct = SimpleNamespace(Name="ST_Data", ItemType=606, DeclarationText="")
        with patch.object(_native_bridge, "_system_manager", return_value=object()), \
             patch.object(
                 _native_bridge, "_find_object",
                 return_value=(struct, "TIPC^PLC^DUTs^ST_Data"),
             ):
            result = _native_bridge._write_pou(object(), {
                "name": "ST_Data", "area": "declaration",
                "code": "STRUCT\n    nValue : INT;\nEND_STRUCT",
            })
        self.assertEqual("written", result["status"])
        self.assertEqual(
            "TYPE ST_Data :\nSTRUCT\n    nValue : INT;\nEND_STRUCT\nEND_TYPE",
            struct.DeclarationText,
        )

    def test_interface_declaration_rejects_inline_members(self) -> None:
        with self.assertRaisesRegex(ValueError, "plc_create_member"):
            plc.validate_interface_declaration(
                "I_Demo", "INTERFACE I_Demo\nMETHOD Start : BOOL"
            )


if __name__ == "__main__":
    unittest.main()
