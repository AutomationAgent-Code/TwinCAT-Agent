from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from tc_agent import dynamic_ads


class DynamicAdsBridgeTests(unittest.TestCase):
    def test_read_symbol_parses_bridge_json(self) -> None:
        completed = Mock(returncode=0, stdout=json.dumps({
            "ok": True, "value": 12, "symbol": {"type": "INT"},
        }) + "\n", stderr="")
        with patch.object(dynamic_ads, "bridge_path") as path, \
             patch.object(dynamic_ads.subprocess, "run", return_value=completed):
            path.return_value.is_file.return_value = True
            path.return_value.__str__.return_value = "bridge.exe"
            result = dynamic_ads.read_symbol("1.2.3.4.1.1", 851, "MAIN.nValue")
        self.assertEqual(12, result["value"])

    def test_bridge_error_is_not_reported_as_a_value(self) -> None:
        completed = Mock(returncode=2, stdout=json.dumps({
            "ok": False, "error": "ADS symbol not found",
        }) + "\n", stderr="")
        with patch.object(dynamic_ads, "bridge_path") as path, \
             patch.object(dynamic_ads.subprocess, "run", return_value=completed):
            path.return_value.is_file.return_value = True
            path.return_value.__str__.return_value = "bridge.exe"
            with self.assertRaisesRegex(dynamic_ads.DynamicAdsError, "symbol not found"):
                dynamic_ads.read_symbol("1.2.3.4.1.1", 851, "MAIN.missing")

    def test_source_registers_runtime_assembly_resolver(self) -> None:
        source = (dynamic_ads.Path(__file__).resolve().parents[1] /
                  "tools" / "TcAdsDynamicProbe.cs").read_text(encoding="utf-8")
        self.assertIn("AssemblyResolve", source)
        self.assertIn("SymbolLoaderSettings.DefaultDynamic", source)
        self.assertIn("Whole-object writes are not allowed", source)
