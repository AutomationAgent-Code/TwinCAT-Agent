import unittest
from unittest.mock import patch

from tc_template import tc_platform as platform


class FakeItem:
    def __init__(self, name, *, nested=None, children=None, xml="", ads_port=None):
        self.Name = name
        self.NestedProject = nested
        self._children = children or []
        self._xml = xml
        self.consumed = []
        self.generated = 0
        if ads_port is not None:
            self.AdsPort = ads_port

    def __iter__(self):
        return iter(self._children)

    def ProduceXml(self, *_args):
        return self._xml

    def ConsumeXml(self, xml):
        self.consumed.append(xml)

    def GenerateBootProject(self, _force):
        self.generated += 1


class FakeSysman:
    def __init__(self, roots):
        self.tipc = FakeItem("TIPC", children=roots)

    def LookupTreeItem(self, path):
        if path == "TIPC":
            return self.tipc
        raise KeyError(path)


class MultiPlcDeployTests(unittest.TestCase):
    def make_sysman(self):
        project1 = FakeItem("PLC1 Project")
        instance1 = FakeItem("PLC1 Instance", ads_port=851)
        root1 = FakeItem("PLC1", nested=project1, children=[instance1])

        project2 = FakeItem("Motion Project")
        instance2 = FakeItem(
            "Motion Instance",
            xml="<TreeItem><IECProjectDef><AmsPort>853</AmsPort></IECProjectDef></TreeItem>",
        )
        root2 = FakeItem("Motion", nested=project2, children=[instance2])
        return FakeSysman([root1, root2]), (root1, root2), (project1, project2)

    def test_enumerates_actual_ports_without_assuming_sequence(self):
        sysman, _roots, _projects = self.make_sysman()
        runtimes = platform._list_plc_runtimes(sysman)
        self.assertEqual([r["ads_port"] for r in runtimes], [851, 853])
        self.assertEqual(runtimes[1]["port_source"], "xml:AmsPort")

    def test_online_command_targets_every_plc(self):
        sysman, _roots, projects = self.make_sysman()
        with patch.object(platform, "_sysman", return_value=sysman), \
             patch.object(platform.time, "sleep"):
            result = platform._send_online_command(all_plcs=True, LoginCmd=True)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["plcs"]), 2)
        self.assertEqual([len(p.consumed) for p in projects], [1, 1])

    def test_boot_project_is_generated_for_every_plc(self):
        sysman, roots, _projects = self.make_sysman()
        with patch.object(platform, "_sysman", return_value=sysman):
            result = platform.set_boot_project()
        self.assertTrue(result["boot_project"])
        self.assertEqual([root.generated for root in roots], [1, 1])
        self.assertEqual([p["ads_port"] for p in result["plcs"]], [851, 853])

    def test_restart_timeout_is_failure(self):
        class RestartSysman:
            def StartRestartTwinCAT(self):
                pass

        with patch.object(platform, "_sysman", return_value=RestartSysman()), \
             patch.object(platform, "_poll_runtime_started", return_value=False):
            result = platform.restart_twincat()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["started"])


if __name__ == "__main__":
    unittest.main()
