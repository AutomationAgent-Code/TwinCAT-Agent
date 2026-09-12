import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FaeCliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "fae.ask", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )

    def test_stats(self):
        result = self.run_cli("stats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"corpus_exists": true', result.stdout)
        self.assertIn('"index_exists": true', result.stdout)

    def test_ask_returns_evidence(self):
        result = self.run_cli("ask", "EtherCAT Simulation", "--limit", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("=== SYSTEM PROMPT ===", result.stdout)
        self.assertIn("[CN1]", result.stdout)
        self.assertIn("Source:", result.stdout)


if __name__ == "__main__":
    unittest.main()
