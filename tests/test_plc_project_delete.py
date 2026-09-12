from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tc_template.plc import _resolve_plc_project_disk_target


def _write_tsproj(path: Path, name: str, project_path: str) -> None:
    path.write_text(
        f'<TcSmProject><Project><Plc><Project Name="{name}" '
        f'PrjFilePath="{project_path}" /></Plc></Project></TcSmProject>',
        encoding="utf-8",
    )


class PlcProjectDeleteTests(unittest.TestCase):
    def test_resolves_dedicated_project_directory_from_tsproj(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tsproj = root / "Machine.tsproj"
            project_file = root / "PLC1" / "PLC1.plcproj"
            project_file.parent.mkdir()
            project_file.write_text("<Project />", encoding="utf-8")
            _write_tsproj(tsproj, "PLC1", r"PLC1\PLC1.plcproj")

            actual_file, actual_dir = _resolve_plc_project_disk_target(tsproj, "PLC1")

            self.assertEqual(project_file.resolve(), actual_file)
            self.assertEqual(project_file.parent.resolve(), actual_dir)

    def test_rejects_project_path_outside_system_project(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            system_dir = root / "System"
            system_dir.mkdir()
            outside = root / "Outside" / "PLC1.plcproj"
            outside.parent.mkdir()
            outside.write_text("<Project />", encoding="utf-8")
            tsproj = system_dir / "Machine.tsproj"
            _write_tsproj(tsproj, "PLC1", r"..\Outside\PLC1.plcproj")

            with self.assertRaisesRegex(ValueError, "escapes"):
                _resolve_plc_project_disk_target(tsproj, "PLC1")

    def test_rejects_directory_shared_by_multiple_plc_projects(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project_dir = root / "PLC"
            project_dir.mkdir()
            (project_dir / "PLC1.plcproj").write_text("", encoding="utf-8")
            (project_dir / "PLC2.plcproj").write_text("", encoding="utf-8")
            tsproj = root / "Machine.tsproj"
            _write_tsproj(tsproj, "PLC1", r"PLC\PLC1.plcproj")

            with self.assertRaisesRegex(ValueError, "other projects"):
                _resolve_plc_project_disk_target(tsproj, "PLC1")
