import unittest
from unittest.mock import patch

from tc_template import tc_platform as platform


class FakeItem:
    def __init__(self, name, path, *, children=None, xml="<TreeItem/>", item_type=0,
                 item_subtype=0):
        self.Name = name
        self.PathName = path
        self.ItemType = item_type
        self.ItemSubType = item_subtype
        self._children = list(children or [])
        self._xml = xml
        self.consumed = []

    def __iter__(self):
        return iter(self._children)

    def ProduceXml(self, *_args):
        return self._xml

    def ConsumeXml(self, xml):
        self.consumed.append(xml)
        self._xml = xml

    def CreateChild(self, name, item_type, _unused, _info):
        child = FakeItem(name, f"{self.PathName}^{name}", item_type=item_type)
        self._children.append(child)
        return child

    def DeleteChild(self, name):
        self._children = [child for child in self._children if child.Name != name]


class FakeSysman:
    def __init__(self):
        tirs_xml = (
            "<TreeItem><System><Settings MaxCpus=\"4\" PCoreAffinity=\"3\" Affinity=\"#x1\" "
            "RouterMemory=\"32768\" MaxStackSize=\"64\">"
            "<TargetCPUInfo><AvailabeCPUs>4</AvailabeCPUs>"
            "<PCoreAffinity>3</PCoreAffinity><ECoreAffinity>12</ECoreAffinity>"
            "<RealTimeCPUs>1</RealTimeCPUs></TargetCPUInfo>"
            "<Cpu CpuId=\"2\"><LoadLimit>80</LoadLimit><BaseTime>10000</BaseTime>"
            "<LatencyWarning>0</LatencyWarning><CpuMemorySize>524288</CpuMemorySize></Cpu>"
            "<Cpu CpuId=\"3\"/>"
            "</Settings><Tasks><Task Id=\"3\" Priority=\"20\" CycleTime=\"100000\">"
            "<Name>PlcTask</Name></Task></Tasks></System></TreeItem>"
        )
        self.tirs = FakeItem("Real-Time Settings", "TIRS", xml=tirs_xml)
        task_xml = ("<TreeItem><Task CpuId=\"2\" Priority=\"20\" CycleTime=\"100000\" "
                    "AutoStart=\"true\" TickModulo=\"0\" ExceedWarning=\"0\" "
                    "WatchdogStackCapacity=\"0\"><Name>PlcTask</Name></Task></TreeItem>")
        self.task = FakeItem("PlcTask", "TIRT^PlcTask", xml=task_xml, item_type=621)
        self.tirt = FakeItem("Real-Time Tasks", "TIRT", children=[self.task], item_type=20)
        self.extra = FakeItem("Custom", "TIRC^Custom", item_type=700)
        self.tirc = FakeItem("Configuration", "TIRC", children=[self.extra], item_type=10)
        self.roots = {"TIRC": self.tirc, "TIRS": self.tirs, "TIRT": self.tirt}

    def LookupTreeItem(self, path):
        if path in self.roots:
            return self.roots[path]
        for root in self.roots.values():
            stack = list(root._children)
            while stack:
                item = stack.pop(0)
                if item.PathName == path:
                    return item
                stack.extend(item._children)
        raise KeyError(path)

    def GetTargetNetId(self):
        return "127.0.0.1.1.1"


class SystemToolTests(unittest.TestCase):
    def setUp(self):
        self.sysman = FakeSysman()
        # Fake SYSTEM XML must never trigger discovery of a user's live XAE.
        version = patch.object(platform, "get_target_tc_version", return_value={
            "version_str": "3.1.4026.0", "build": 4026, "revision": 0,
            "source": "test_fixture",
        })
        version.start()
        self.addCleanup(version.stop)

    def test_structure_reads_only_requested_system_roots(self):
        result = platform.system_structure(roots=["TIRT"], sysman=self.sysman)
        self.assertEqual(["TIRT"], [item["path"] for item in result["roots"]])
        self.assertEqual("PlcTask", result["roots"][0]["children"][0]["name"])

    def test_settings_reads_core_ids_affinity_and_tasks(self):
        result = platform.system_settings(sysman=self.sysman)
        self.assertEqual(4, result["max_cpus"])
        self.assertEqual([2, 3], result["cpu_ids"])
        self.assertEqual(3, result["p_core_affinity"])
        self.assertEqual("PlcTask", result["tasks"][0]["Name"])

    def test_settings_expands_p_and_e_core_labels(self):
        result = platform.system_settings(sysman=self.sysman)
        self.assertEqual(4, result["available_cpus"])
        self.assertEqual(1, result["real_time_cpus"])
        self.assertEqual(4, len(result["cores"]))
        self.assertEqual(
            ["0 (P)", "1 (P)", "2 (E)", "3 (E)"],
            [core["label"] for core in result["cores"]],
        )
        self.assertTrue(result["cores"][0]["selected"])
        self.assertFalse(result["cores"][1]["selected"])
        self.assertTrue(result["cores"][2]["configured"])
        self.assertEqual(1000, result["cores"][2]["base_time_us"])
        self.assertEqual(512, result["cores"][2]["core_memory_kb"])

    def test_settings_distinguishes_4024_and_4026_memory_semantics(self):
        xml = self.sysman.tirs.ProduceXml(False)
        old = platform._system_settings_summary(
            xml, {"version_str": "3.1.4024.67", "build": 4024, "revision": 67,
                  "source": "project"})
        new = platform._system_settings_summary(
            xml, {"version_str": "3.1.4026.18", "build": 4026, "revision": 18,
                  "source": "project"})
        self.assertEqual("combined_global_rt_and_ads_memory",
                         old["twincat"]["router_memory_semantics"])
        self.assertFalse(old["twincat"]["core_memory_supported"])
        self.assertEqual("global_rt_memory_ads_separate",
                         new["twincat"]["router_memory_semantics"])
        self.assertEqual(8, new["global_ads_memory_estimated_mb"])

    def test_realtime_memory_and_core_settings_are_previewed_in_user_units(self):
        with patch.object(platform, "get_target_tc_version", return_value={
            "version_str": "3.1.4026.18", "build": 4026, "revision": 18,
            "source": "project",
        }):
            result = platform.realtime_settings_set({
                "router_memory_mb": 64,
                "max_stack_size_kb": 128,
                "core_settings": [{"cpu_id": 2, "core_memory_kb": 1024,
                                   "base_time_100ns": 10000}],
            }, apply=False, sysman=self.sysman)
        self.assertEqual(65536, result["requested"]["RouterMemory"])
        self.assertEqual(1048576, result["requested"]["CoreSettings"][0]["CpuMemorySize"])
        self.assertTrue(result["requires_restart_for_ads_memory"])

    def test_realtime_validator_accepts_consistent_4026_snapshot(self):
        result = platform.validate_realtime_snapshot({
            "twincat": {"family": "4026"},
            "memory": {"router_memory_mb": 32, "max_task_stack_kb": 64},
            "cores": [{
                "id": 12, "selected": True, "configured": True,
                "base_time_100ns": 10000, "load_limit_percent": 80,
                "core_memory_bytes": 524288,
            }],
            "tasks": [{
                "name": "PlcTask", "path": "TIRT^PlcTask", "priority": 20,
                "cycle_time_100ns": 100000, "cpu_affinity": 1 << 12,
            }],
        })
        self.assertTrue(result["valid"])
        self.assertEqual(0, result["error_count"])
        self.assertEqual(64, result["summary"]["estimated_task_stack_kb"])

    def test_realtime_validator_reports_priority_and_cycle_errors(self):
        result = platform.validate_realtime_snapshot({
            "twincat": {"family": "4024"},
            "memory": {"router_memory_mb": 2048, "max_task_stack_kb": 64},
            "cores": [{
                "id": 0, "selected": True, "configured": True,
                "base_time_100ns": 10000, "load_limit_percent": 80,
                "core_memory_bytes": 1024,
            }],
            "tasks": [
                {"name": "A", "priority": 20, "cycle_time_100ns": 5000,
                 "cpu_affinity": 1},
                {"name": "B", "priority": 20, "cycle_time_100ns": 10000,
                 "cpu_affinity": 1},
            ],
        })
        self.assertFalse(result["valid"])
        codes = {item["code"] for item in result["issues"]}
        self.assertTrue({"router_memory_4024_limit", "core_memory_unsupported_4024",
                         "task_priority_duplicate", "task_cycle_base_time_mismatch"}.issubset(codes))

    def test_4024_rejects_core_memory_and_router_memory_above_1gb(self):
        with patch.object(platform, "get_target_tc_version", return_value={
            "version_str": "3.1.4024.67", "build": 4024, "revision": 67,
            "source": "project",
        }):
            with self.assertRaisesRegex(ValueError, "4024"):
                platform.realtime_settings_set(
                    {"core_settings": [{"cpu_id": 2, "core_memory_kb": 512}]},
                    sysman=self.sysman)
            with self.assertRaisesRegex(ValueError, "1024"):
                platform.realtime_settings_set(
                    {"router_memory_mb": 2048}, sysman=self.sysman)

    def test_settings_set_previews_then_applies_and_reads_back(self):
        preview = platform.system_settings_set(
            {"max_cpus": 4, "cpu_ids": [0, 1]}, apply=False, sysman=self.sysman)
        self.assertEqual("preview", preview["status"])
        self.assertFalse(self.sysman.tirs.consumed)

        result = platform.system_settings_set(
            {"max_cpus": 4, "cpu_ids": [0, 1]}, apply=True, sysman=self.sysman)
        self.assertEqual("written", result["status"])
        self.assertTrue(result["readback_verified"])
        self.assertEqual([0, 1], result["after"]["cpu_ids"])
        self.assertIn("LoadLimit", result["after"]["xml"])

    def test_core_assignment_rejects_core_outside_max_cpus(self):
        with self.assertRaisesRegex(ValueError, "max_cpus"):
            platform.core_assign([4], max_cpus=4, sysman=self.sysman)

    def test_core_assignment_generates_overall_affinity_from_selected_ids(self):
        preview = platform.core_assign(
            [0, 2, 3], max_cpus=4, apply=False, sysman=self.sysman)
        self.assertEqual("preview", preview["status"])
        self.assertEqual(0xD, preview["requested"]["Affinity"])
        self.assertEqual(0xD, preview["after"]["affinity"])

    def test_task_core_assignment_is_previewed_and_verified(self):
        preview = platform.task_core_assign(
            "TIRT^PlcTask", 3, apply=False, sysman=self.sysman)
        self.assertEqual("preview", preview["status"])
        self.assertEqual(2, preview["before"]["cpu_id"])
        result = platform.task_core_assign(
            "TIRT^PlcTask", 3, apply=True, sysman=self.sysman)
        self.assertTrue(result["readback_verified"])
        self.assertEqual(3, result["readback_cpu_id"])

    def test_task_settings_validate_base_time_and_read_back(self):
        with self.assertRaisesRegex(ValueError, "base time"):
            platform.task_settings_set(
                "TIRT^PlcTask", {"cycle_time_100ns": 2000}, sysman=self.sysman)
        result = platform.task_settings_set(
            "TIRT^PlcTask", {"cycle_time_us": 20000, "priority": 21},
            apply=True, sysman=self.sysman)
        self.assertEqual("written", result["status"])
        self.assertTrue(result["readback_verified"])

    def test_system_add_and_remove_are_exact_and_previewed(self):
        preview = platform.system_add(
            "TIRT", "Task_1ms", 621, apply=False, sysman=self.sysman)
        self.assertEqual("preview", preview["status"])
        self.assertEqual("TIRT^Task_1ms", preview["path"])

        result = platform.system_add(
            "TIRT", "Task_1ms", 621, apply=True, sysman=self.sysman)
        self.assertEqual("created", result["status"])
        remove_preview = platform.system_remove(
            "TIRT^Task_1ms", apply=False, sysman=self.sysman)
        self.assertEqual("preview", remove_preview["status"])
        removed = platform.system_remove(
            "TIRT^Task_1ms", apply=True, sysman=self.sysman)
        self.assertEqual("deleted", removed["status"])

    def test_system_mutation_paths_cannot_escape_allowed_roots(self):
        with self.assertRaisesRegex(ValueError, "must start"):
            platform.system_add("TIID", "Bad", 1, sysman=self.sysman)
        with self.assertRaisesRegex(ValueError, "must start"):
            platform.system_remove("TIPC^PLC1", sysman=self.sysman)


if __name__ == "__main__":
    unittest.main()
