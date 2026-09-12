from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_agent import plc_versions


def area(text: str, digest: str) -> dict:
    return {"hash": digest, "lines": len(text.splitlines()), "chars": len(text), "text": text}


def inventory(solution: str, impl: str = "x := 1;", digest: str = "old") -> dict:
    path = "TIPC^PLC^Project^POUs^FBs^FB_Test"
    return {
        "solution": solution,
        "project_path": "TIPC^PLC^Project",
        "generated_at": "2026-08-04T00:00:00Z",
        "include_code": True,
        "object_count": 1,
        "objects": [{
            "name": "FB_Test", "path": path, "folder": "POUs",
            "itemType": 604, "kind": "function_block",
            "declaration": area("FUNCTION_BLOCK FB_Test", "decl"),
            "implementation": area(impl, digest),
            "members": [],
        }],
    }


class PlcVersionTests(unittest.TestCase):
    def test_snapshot_changed_and_diff_are_project_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = str(Path(temp) / "Machine.sln")
            Path(solution).touch()
            baseline = inventory(solution)
            with patch("tc_agent.plc_versions._inventory", return_value=baseline):
                created = plc_versions.create_snapshot("before-change")

            snapshot_file = Path(created["file"])
            self.assertTrue(snapshot_file.is_file())
            self.assertEqual("snapshots", snapshot_file.parent.name)
            self.assertEqual(".TwinCATAgent", snapshot_file.parent.parent.name)

            current_hashes = inventory(solution, impl="", digest="new")
            for obj in current_hashes["objects"]:
                obj["declaration"]["text"] = None
                obj["implementation"]["text"] = None
            with patch("tc_agent.plc_versions._inventory", return_value=current_hashes):
                result = plc_versions.changed("latest")
            self.assertEqual(1, result["counts"]["modified"])
            self.assertEqual(["implementation"], result["changes"][0]["areas"])

            current_code = inventory(solution, impl="x := 2;", digest="new")
            with patch(
                "tc_agent.plc_versions._inventory",
                side_effect=[current_hashes, current_code],
            ):
                result = plc_versions.diff("latest", name="FB_Test")
            self.assertEqual(1, result["change_count"])
            self.assertEqual(1, result["hunk_count"])
            text = result["hunks"][0]["diff"]
            self.assertIn("-x := 1;", text)
            self.assertIn("+x := 2;", text)

    def test_unchanged_uses_hashes_without_loading_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = str(Path(temp) / "Machine.sln")
            Path(solution).touch()
            baseline = inventory(solution)
            with patch("tc_agent.plc_versions._inventory", return_value=baseline):
                plc_versions.create_snapshot("clean")
            hashes = inventory(solution)
            for obj in hashes["objects"]:
                obj["declaration"]["text"] = None
                obj["implementation"]["text"] = None
            with patch("tc_agent.plc_versions._inventory", return_value=hashes) as mocked:
                result = plc_versions.diff("latest")
            self.assertEqual(0, result["change_count"])
            self.assertEqual(1, mocked.call_count)

    def test_powershell_inventory_hashes_inside_com_process(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "tc_template" / "TcCom.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("function Get-CodeInventory", source)
        self.assertIn("function Get-CodeHash", source)
        self.assertIn("'code-inventory'", source)
        self.assertIn("[Security.Cryptography.SHA256]::Create()", source)

    def test_restore_preview_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = str(Path(temp) / "Machine.sln")
            Path(solution).touch()
            baseline = inventory(solution)
            with patch("tc_agent.plc_versions._inventory", return_value=baseline):
                plc_versions.create_snapshot("restore-me")
            current = inventory(solution, impl="x := 2;", digest="new")
            with patch("tc_agent.plc_versions._inventory", return_value=current), \
                 patch("tc_agent.plc_versions._write_snapshot_code") as write:
                result = plc_versions.restore_snapshot("latest")
            self.assertEqual("preview", result["status"])
            self.assertTrue(result["would_write"])
            write.assert_not_called()

    def test_restore_rolls_back_when_build_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = str(Path(temp) / "Machine.sln")
            Path(solution).touch()
            baseline = inventory(solution)
            with patch("tc_agent.plc_versions._inventory", return_value=baseline):
                plc_versions.create_snapshot("restore-me")
            current = inventory(solution, impl="x := 2;", digest="new")
            with patch("tc_agent.plc_versions._inventory", return_value=current), \
                 patch("tc_agent.plc_versions.create_snapshot",
                       return_value={"status": "created", "file": "backup"}), \
                 patch("tc_agent.plc_versions._write_snapshot_code",
                       side_effect=[1, 1]) as write, \
                 patch("tc_agent.plc_versions.com_build", side_effect=[
                     {"failedProjects": 1, "errorCount": 2},
                     {"failedProjects": 0, "errorCount": 0},
                 ]):
                result = plc_versions.restore_snapshot("latest", apply=True)
            self.assertEqual("rolled_back", result["status"])
            self.assertEqual(2, write.call_count)

    def test_restore_blocks_structural_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = str(Path(temp) / "Machine.sln")
            Path(solution).touch()
            baseline = inventory(solution)
            with patch("tc_agent.plc_versions._inventory", return_value=baseline):
                plc_versions.create_snapshot("restore-me")
            current = inventory(solution)
            current["objects"] = []
            current["object_count"] = 0
            with patch("tc_agent.plc_versions._inventory", return_value=current):
                result = plc_versions.restore_snapshot("latest", apply=True)
            self.assertEqual("blocked", result["status"])
            self.assertEqual("object", result["structural_changes"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
