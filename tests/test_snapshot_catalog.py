from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tc_agent import backend
from tc_agent import plc_versions


def _area(text: str, digest: str = "hash") -> dict:
    return {"hash": digest, "lines": len(text.splitlines()), "chars": len(text), "text": text}


def _payload(solution: str, *, name: str = "baseline", text: str = "x := 1;") -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "created_at": "2026-09-13T00:00:00+00:00",
        "solution": solution,
        "object_count": 1,
        "objects": [{
            "name": "FB_Test",
            "path": "TIPC^PLC^Project^POUs^FBs^FB_Test",
            "folder": "POUs",
            "itemType": 604,
            "kind": "function_block",
            "declaration": _area("FUNCTION_BLOCK FB_Test", "decl"),
            "implementation": _area(text, "impl"),
            "members": [{
                "name": "Run",
                "path": "TIPC^PLC^Project^POUs^FBs^FB_Test^Run",
                "kind": "method",
                "declaration": _area("METHOD Run", "member-decl"),
                "implementation": _area(text, "member-impl"),
            }],
        }],
    }


def _write_snapshot(root: Path, filename: str, payload: dict) -> Path:
    path = root / filename
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class SnapshotCatalogTests(unittest.TestCase):
    def test_late_scope_response_is_rejected_after_project_or_pid_switch(self) -> None:
        self.assertTrue(backend.snapshot_scope_matches("C:/A/Machine.sln", 12, "C:/A/Machine.sln", 12))
        self.assertFalse(backend.snapshot_scope_matches("C:/A/Machine.sln", 12, "C:/B/Other.sln", 12))
        self.assertFalse(backend.snapshot_scope_matches("C:/A/Machine.sln", 12, "C:/A/Machine.sln", 13))

    def test_empty_catalog_reports_missing_directory_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = Path(temp) / "Machine.sln"
            solution.touch()
            before = sorted(Path(temp).rglob("*"))

            result = plc_versions.list_snapshots_for_solution(str(solution))

            self.assertFalse(result["directory_exists"])
            self.assertEqual(0, result["total"])
            self.assertEqual([], result["snapshots"])
            self.assertEqual(before, sorted(Path(temp).rglob("*")))

    def test_catalog_details_and_content_are_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = Path(temp) / "Machine.sln"
            solution.touch()
            root = solution.parent / ".TwinCATAgent" / "snapshots"
            root.mkdir(parents=True)
            large = "0123456789" * 300
            payload = _payload(str(solution), text=large)
            payload.pop("name")
            payload["objects"][0].pop("kind")
            valid = _write_snapshot(root, "20260913T000000Z-baseline.json.gz", payload)
            (root / "broken.json.gz").write_bytes(b"not gzip")
            before = hashlib.sha256(valid.read_bytes()).hexdigest()

            catalog = plc_versions.list_snapshots_for_solution(str(solution), limit=1)
            self.assertEqual(2, catalog["total"])
            self.assertTrue(catalog["has_more"])
            self.assertEqual(1, catalog["count"])
            self.assertIn("category", catalog["snapshots"][0])

            detail = plc_versions.snapshot_detail(str(solution), valid.name)
            self.assertEqual(valid.name, detail["snapshot"])
            self.assertEqual(1, detail["object_total"])
            obj = detail["objects"][0]
            self.assertFalse(obj["kind"])
            self.assertTrue(obj["implementation"]["available"])
            self.assertEqual(1, len(obj["members"]))

            first = plc_versions.snapshot_content(
                str(solution), valid.name, obj["path"], area="implementation", max_chars=1000,
            )
            self.assertEqual(1000, len(first["content"]))
            self.assertIsNotNone(first["next_offset"])
            second = plc_versions.snapshot_content(
                str(solution), valid.name, obj["path"], area="implementation",
                offset=first["next_offset"], max_chars=1000,
            )
            self.assertEqual(first["content"] + second["content"], large[:2000])
            self.assertEqual(before, hashlib.sha256(valid.read_bytes()).hexdigest())

    def test_catalog_reports_corrupt_snapshot_and_rejects_foreign_or_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = Path(temp) / "Machine.sln"
            other = Path(temp) / "Other.sln"
            solution.touch()
            other.touch()
            root = solution.parent / ".TwinCATAgent" / "snapshots"
            root.mkdir(parents=True)
            foreign = _write_snapshot(root, "foreign.json.gz", _payload(str(other)))

            catalog = plc_versions.list_snapshots_for_solution(str(solution))
            self.assertEqual(1, catalog["total"])
            self.assertIn("error", catalog["snapshots"][0])
            with self.assertRaisesRegex(RuntimeError, "路径|引用"):
                plc_versions.snapshot_detail(str(solution), "..\\foreign.json.gz")
            with self.assertRaisesRegex(RuntimeError, "不属于当前解决方案"):
                plc_versions.snapshot_detail(str(solution), foreign.name)

    def test_snapshot_content_rejects_unknown_object_and_area(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            solution = Path(temp) / "Machine.sln"
            solution.touch()
            root = solution.parent / ".TwinCATAgent" / "snapshots"
            root.mkdir(parents=True)
            path = _write_snapshot(root, "one.json.gz", _payload(str(solution)))
            with self.assertRaisesRegex(ValueError, "declaration"):
                plc_versions.snapshot_content(str(solution), path.name, "missing", area="text")
            with self.assertRaisesRegex(RuntimeError, "找不到指定 PLC 对象"):
                plc_versions.snapshot_content(str(solution), path.name, "missing")


if __name__ == "__main__":
    unittest.main()
