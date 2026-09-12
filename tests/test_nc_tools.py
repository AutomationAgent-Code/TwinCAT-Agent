import unittest
from unittest.mock import patch

from tc_template import tc_platform as platform


class FakeItem:
    def __init__(self, name, path, *, children=None, xml="<TreeItem/>", item_type=0):
        self.Name = name
        self.PathName = path
        self.ItemType = item_type
        self.ItemSubType = 1
        self.ItemSubTypeName = ""
        self._children = children or []
        self._xml = xml
        self.consumed = []

    def __iter__(self):
        return iter(self._children)

    def ProduceXml(self, *_args):
        return self._xml

    def ConsumeXml(self, xml):
        self.consumed.append(xml)
        self._xml = xml

    def CreateChild(self, name, *_args):
        path = f"{self.PathName}^{name}"
        child = FakeItem(name, path)
        self._children.append(child)
        return child


class FakeSysman:
    def __init__(self):
        self.variable_links = []
        drive_vars = [FakeItem(name, f"TINC^Axis 1^Drive^{name}") for name in (
            "nDataIn1", "nDcOutputTime", "nState1", "nState2", "nState4",
            "nCtrl1", "nDataOut1",
        )]
        enc_vars = [FakeItem(name, f"TINC^Axis 1^Enc^{name}") for name in (
            "nDataIn1", "nDcInputTime", "nState4",
        )]
        self.axis = FakeItem(
            "Axis 1", "TINC^NC-Task^Axes^Axis 1",
            children=[
                FakeItem("Drive", "TINC^NC-Task^Axes^Axis 1^Drive", children=drive_vars),
                FakeItem("Enc", "TINC^NC-Task^Axes^Axis 1^Enc", children=enc_vars),
            ],
            xml=("<TreeItem><NcAxisDef><AxisType>1</AxisType>"
                 "<ScaleFactor>0.001</ScaleFactor></NcAxisDef></TreeItem>"),
        )
        self.axes = FakeItem("Axes", "TINC^NC-Task^Axes", children=[self.axis])
        self.task = FakeItem("NC-Task", "TINC^NC-Task", children=[self.axes])
        self.nc = FakeItem("NC", "TINC", children=[self.task])
        self.drive = FakeItem(
            "EL7211 Drive", "TIID^Master^EL7211 Drive", item_type=5,
            children=[
                FakeItem("AT 1", "TIID^Master^EL7211 Drive^AT 1"),
                FakeItem("MDT 1", "TIID^Master^EL7211 Drive^MDT 1"),
            ],
        )
        self.encoder = FakeItem("EL5101 Encoder", "TIID^Master^EL5101 Encoder", item_type=5)
        self.io = FakeItem("I/O", "TIID", children=[self.drive, self.encoder])

    def LookupTreeItem(self, path):
        items = [self.nc, self.task, self.axes, self.axis, self.io,
                 self.drive, self.encoder]
        for item in items:
            if item.PathName == path:
                return item
        raise KeyError(path)

    def GetTargetNetId(self):
        return "127.0.0.1.1.1"

    def ProduceMappingInfo(self):
        return f"""<MappingInfo><OwnerA Name="TIPC^PLC^PLC Instance">
          <OwnerB Name="{self.axis.PathName}">
            <Link VarA="PlcTask Inputs^MAIN.Axis1.NcToPlc" VarB="Outputs^ToPlc"/>
            <Link VarA="PlcTask Outputs^MAIN.Axis1.PlcToNc" VarB="Inputs^FromPlc"/>
          </OwnerB></OwnerA></MappingInfo>"""

    def LinkVariables(self, left, right, *bit_args):
        self.variable_links.append((left, right, bit_args))
        return 0


class NcToolTests(unittest.TestCase):
    def setUp(self):
        self.sysman = FakeSysman()
        self.sysman_patch = patch.object(platform, "_sysman", return_value=self.sysman)
        self.sysman_patch.start()
        self.addCleanup(self.sysman_patch.stop)

    def test_structure_and_axis_parameters(self):
        result = platform.nc_structure()
        self.assertEqual("NC", result["tree"]["name"])
        params = platform.nc_axis_params("Axis 1")["parameters"]
        self.assertEqual("0.001", params["ScaleFactor"])

    def test_create_task_and_axis_are_idempotent(self):
        self.assertEqual("skipped", platform.nc_create_task("NC-Task")["status"])
        with patch.object(platform.time, "sleep"):
            result = platform.nc_create_axis("NC-Task", "Axis 2")
        self.assertEqual("Axis 2", result["axis"]["name"])
        self.assertEqual("skipped", platform.nc_create_axis("NC-Task", "Axis 2")["status"])

    def test_set_axis_parameters_writes_and_verifies_allow_list(self):
        with patch.object(platform.time, "sleep"):
            result = platform.nc_set_axis_params(
                "Axis 1", {"Acceleration": 1500, "Jerk": 2250, "Modulo": False}
            )
        self.assertTrue(result["verified"])
        self.assertEqual("1500", result["after"]["Acceleration"])
        self.assertEqual("0", result["after"]["Modulo"])

    def test_set_axis_parameters_rejects_unknown_field(self):
        with self.assertRaisesRegex(ValueError, "unsupported NC axis parameter"):
            platform.nc_set_axis_params("Axis 1", {"UnsafeXml": 1})

    def test_task_base_name_resolves_twincat_saf_suffix(self):
        self.sysman.task.Name = "NC-Task SAF"
        self.assertEqual("skipped", platform.nc_create_task("NC-Task")["status"])
        self.assertIs(self.sysman.task, platform._find_nc_item("NC-Task"))

    def test_drive_and_encoder_discovery(self):
        drive = platform.find_servo_drives()[0]
        self.assertEqual("EL7211 Drive", drive["name"])
        self.assertEqual([1], drive["channels"])
        self.assertEqual("EL5101 Encoder", platform.find_nc_encoders()[0]["name"])

    def test_unreferenced_sync_unit_is_not_an_encoder(self):
        self.sysman.io._children.append(FakeItem(
            "<unreferenced>", "TIID^SyncUnits^<default>^<unreferenced>"
        ))
        names = [item["name"] for item in platform.find_nc_encoders()]
        self.assertNotIn("<unreferenced>", names)

    def test_position_command_is_not_an_encoder(self):
        self.sysman.io._children.append(FakeItem(
            "Position command value", "TIID^Drive^MDT 1^Position command value"
        ))
        names = [item["name"] for item in platform.find_nc_encoders()]
        self.assertNotIn("Position command value", names)

    def test_links_use_nested_encoder_io_item_and_are_reported(self):
        with patch.object(platform.time, "sleep"):
            result = platform.nc_link_encoder("Axis 1", self.sysman.encoder.PathName)
        self.assertTrue(result["verified"])
        self.assertIn("<Encoder><IoItem>", self.sysman.axis.consumed[-1])
        links = platform.nc_links()["axes"]
        self.assertEqual(["Axis 1"], [axis["name"] for axis in links])
        self.assertEqual([self.sysman.encoder.PathName], links[0]["encoder_io_paths"])

    def test_internal_drive_node_is_not_reported_as_external_link(self):
        self.sysman.axis._xml = (
            "<TreeItem><NcAxisDef><PathName>TINC^Axis 1</PathName>"
            "<Drive DrvType='5'/></NcAxisDef></TreeItem>"
        )
        self.assertFalse(platform.nc_axis_info("Axis 1")["axis"]["has_drive_link"])

    def test_ax5000_uses_nc_settings_link_not_manual_pdo_mapping(self):
        result = platform.nc_link_drive("Axis 1", self.sysman.drive.PathName, 1)
        self.assertEqual("nc-settings-link", result["strategy"])
        self.assertEqual(1, result["channel_hint"])
        self.assertEqual("handled-by-twincat", result["channel_mapping"])
        self.assertEqual([], self.sysman.variable_links)
        self.assertIn(self.sysman.drive.PathName, self.sysman.axis.consumed[-1])

    def test_el7201_without_at_mdt_uses_coe_device_link(self):
        terminal = FakeItem(
            "EL7201-0010", "TIID^Master^EL7201-0010", item_type=5,
            children=[
                FakeItem("STM Status", "TIID^Master^EL7201-0010^STM Status"),
                FakeItem("STM Control", "TIID^Master^EL7201-0010^STM Control"),
            ],
        )
        self.sysman.io._children.append(terminal)
        original_lookup = self.sysman.LookupTreeItem

        def lookup(path):
            if path == terminal.PathName:
                return terminal
            return original_lookup(path)

        self.sysman.LookupTreeItem = lookup
        with patch.object(platform.time, "sleep"):
            result = platform.nc_link_drive("Axis 1", terminal.PathName, 1)
        self.assertEqual("nc-settings-link", result["strategy"])
        self.assertEqual("handled-by-twincat", result["channel_mapping"])
        self.assertTrue(result["verified"])
        self.assertEqual([], self.sysman.variable_links)
        self.assertIn(terminal.PathName, self.sysman.axis.consumed[-1])

    def test_quick_link_selects_unique_unlinked_axis_and_drive(self):
        with patch.object(platform.time, "sleep"):
            result = platform.nc_quick_link()
        self.assertTrue(result["quick_link"])
        self.assertEqual("Axis 1", result["selected_axis"]["name"])
        self.assertEqual("EL7211 Drive", result["selected_drive"]["name"])

    def test_manifest_validation_reports_missing_axis(self):
        manifest = {"tasks": [{"name": "NC-Task", "axes": [
            {"name": "Axis 1"}, {"name": "Axis 9"},
        ]}]}
        result = platform.nc_validate(manifest)
        self.assertFalse(result["valid"])
        self.assertEqual("Axis 9", result["missing"][0]["name"])

    def test_axis_state_reports_offline_with_discovered_symbol_and_port(self):
        instance = FakeItem("PLC Instance", "TIPC^PLC^PLC Instance")
        instance.AdsPort = 851
        runtime = {
            "instance": instance,
            "ads_port": 851,
        }
        with patch.object(platform, "_list_plc_runtimes", return_value=[runtime]), \
             patch("tc_agent.ads.read_ads_state", side_effect=RuntimeError("ADS 6")):
            result = platform.nc_axis_state("Axis 1")
        self.assertEqual("offline", result["status"])
        self.assertFalse(result["runtime_verified"])
        self.assertEqual("MAIN.Axis1", result["plc_symbol"])
        self.assertEqual(851, result["ads_port"])

    def test_axis_state_reads_and_decodes_live_axis_values(self):
        instance = FakeItem("PLC Instance", "TIPC^PLC^PLC Instance")
        instance.AdsPort = 851
        runtime = {
            "instance": instance,
            "ads_port": 851,
        }

        def values(_target, _port, symbols):
            result = {name: 0 for name in symbols}
            result["MAIN.Axis1.NcToPlc.StateDWord"] = 0b10111
            result["MAIN.Axis1.NcToPlc.ActPos"] = 12.5
            return result

        with patch.object(platform, "_list_plc_runtimes", return_value=[runtime]), \
             patch("tc_agent.ads.read_ads_state", return_value={"state_name": "Run"}), \
             patch("tc_agent.ads.read_ads_values_by_name", side_effect=values):
            result = platform.nc_axis_state("Axis 1")
        self.assertTrue(result["runtime_verified"])
        self.assertEqual(12.5, result["runtime"]["ActPos"])
        self.assertTrue(result["runtime"]["flags"]["operational"])
        self.assertTrue(result["runtime"]["flags"]["homed"])
        self.assertTrue(result["runtime"]["flags"]["not_moving"])
        self.assertTrue(result["runtime"]["flags"]["in_target_position"])

    def test_physical_axis_move_requires_explicit_confirmation(self):
        live = {
            "runtime_verified": True,
            "axis": {"name": "Axis 1", "path": self.sysman.axis.PathName,
                     "has_drive_link": True, "parameters": {}},
            "runtime": {"flags": {"error": False, "homed": True}, "ErrorCode": 0},
            "plc_symbol": "MAIN.Axis1", "target": "127.0.0.1.1.1", "ads_port": 851,
        }
        with patch.object(platform, "nc_axis_state", return_value=live):
            result = platform.nc_axis_move("Axis 1", 10, 5)
        self.assertEqual("confirmation_required", result["status"])
        self.assertTrue(result["physical_axis"])


if __name__ == "__main__":
    unittest.main()
