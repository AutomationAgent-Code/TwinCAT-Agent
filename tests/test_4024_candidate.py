from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VSIX_NS = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}


class TwinCAT4024CandidateTests(unittest.TestCase):
    def _assert_4024_manifest(self, relative: str) -> None:
        root = ET.parse(ROOT / relative).getroot()
        target = root.find(".//v:InstallationTarget", VSIX_NS)
        self.assertIsNotNone(target)
        # TwinCAT's VSIX host exposes the VS Community-compatible 15.x
        # registration surface; targeting TcXaeShell directly caused the
        # extension to disappear from the XAE View menu on the known-good
        # installation.
        self.assertEqual(target.attrib["Id"], "Microsoft.VisualStudio.Community")
        self.assertEqual(target.attrib["Version"], "[15.0,16.0)")
        prerequisite = root.find(".//v:Prerequisite", VSIX_NS)
        self.assertIsNotNone(prerequisite)
        self.assertEqual(
            prerequisite.attrib["Id"], "Microsoft.VisualStudio.Component.CoreEditor"
        )
        self.assertEqual(prerequisite.attrib["Version"], "[15.0,16.0)")

    def test_source_and_staged_manifests_target_shell_15_x86(self) -> None:
        self._assert_4024_manifest("tc_agent_vsix/source.extension.vsixmanifest")
        self._assert_4024_manifest("tc_agent_vsix/deploy.extension.vsixmanifest")

    def test_project_targets_compatible_framework_and_shell(self) -> None:
        source = (ROOT / "tc_agent_vsix/TwinCATAgent.Xae.csproj").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("<TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion>", source)
        self.assertIn('Reference Include="Microsoft.VisualStudio.Shell.15.0"', source)
        self.assertIn("win-x86", source)

    def test_vm_preflight_is_read_only(self) -> None:
        source = (ROOT / "scripts/Test-TwinCAT4024Environment.ps1").read_text(
            encoding="utf-8-sig"
        )
        forbidden = (
            r"\bSet-ItemProperty\b",
            r"\bNew-ItemProperty\b",
            r"\bRemove-ItemProperty\b",
            r"ActivateConfiguration",
            r"StartRestartTwinCAT",
            r"StaticRoutes\.xml",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, source, re.IGNORECASE), pattern)
        self.assertIn(
            r"C:\TwinCAT\3.1\Components\Base\TcXaeShell",
            source,
        )

    def test_candidate_builder_validates_architecture(self) -> None:
        source = (ROOT / "scripts/build_4024_candidate.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Assert-4024Manifest", source)
        self.assertIn("Get-PeArchitecture", source)
        self.assertIn("TwinCAT-Agent-4024-TestKit-v$AppVersion.zip", source)

    def test_portable_build_refreshes_staged_manifest(self) -> None:
        source = (ROOT / "scripts/build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("tc_agent_vsix\\deploy.extension.vsixmanifest", source)


if __name__ == "__main__":
    unittest.main()
