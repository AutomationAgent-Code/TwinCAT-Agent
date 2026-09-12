from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from tc_template.cli import main


class CliExitCodeTests(unittest.TestCase):
    def test_runtime_commands_fail_closed_on_unverified_result(self):
        for verb in ('login', 'logout', 'start', 'stop', 'online'):
            with self.subTest(verb=verb), patch('tc_template._ps_bridge.com_' + verb,
                                               return_value={'status': 'incomplete', 'verified': False}):
                result = CliRunner().invoke(main, ['tc', verb, '--runtime', 'PLC2'])
            self.assertNotEqual(0, result.exit_code)
            self.assertIn('incomplete', result.output)

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_cli_version_comes_from_repository_version_file(self) -> None:
        result = self.runner.invoke(main, ["--version"])

        self.assertEqual(0, result.exit_code, result.output)
        expected = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
        self.assertIn(expected, result.output)

    def test_help_contract_for_public_command_groups(self) -> None:
        """Every public group must expose help without touching TwinCAT or disk state."""
        command_paths = [
            [],
            ["plc"],
            ["tc"],
            ["tc", "target"],
            ["tc", "platform"],
            ["tc", "version"],
            ["tc", "link"],
            ["tc", "nc"],
            ["diag"],
            ["fblib"],
            ["case"],
        ]

        for path in command_paths:
            with self.subTest(command=" ".join(path) or "root"):
                result = self.runner.invoke(main, [*path, "--help"])
                self.assertEqual(0, result.exit_code, result.output)
                self.assertIn("Usage:", result.output)
                self.assertIn("Commands:", result.output)

    def test_representative_leaf_help_is_offline_and_successful(self) -> None:
        leaf_commands = [
            ["list"],
            ["plc", "write"],
            ["plc", "build"],
            ["plc", "analyze"],
            ["tc", "deploy"],
            ["tc", "target", "add"],
            ["tc", "link", "add"],
            ["tc", "nc", "axis-move"],
            ["diag", "target"],
            ["fblib", "add"],
            ["case", "validate"],
        ]

        for path in leaf_commands:
            with self.subTest(command=" ".join(path)):
                result = self.runner.invoke(main, [*path, "--help"])
                self.assertEqual(0, result.exit_code, result.output)
                self.assertIn("Usage:", result.output)

    def test_unknown_commands_have_click_usage_exit_code(self) -> None:
        command_paths = [
            ["not-a-command"],
            ["plc", "not-a-command"],
            ["tc", "not-a-command"],
            ["tc", "target", "not-a-command"],
        ]

        for args in command_paths:
            with self.subTest(command=" ".join(args)):
                result = self.runner.invoke(main, args)
                self.assertEqual(2, result.exit_code, result.output)
                self.assertIn("Usage:", result.output)

    def test_missing_required_arguments_have_click_usage_exit_code(self) -> None:
        commands_requiring_arguments = [
            ["info"],
            ["plc", "read"],
            ["plc", "write"],
            ["tc", "device-read"],
            ["tc", "target", "set"],
            ["tc", "link", "add"],
            ["fblib", "info"],
            ["case", "info"],
        ]

        for args in commands_requiring_arguments:
            with self.subTest(command=" ".join(args)):
                result = self.runner.invoke(main, args)
                self.assertEqual(2, result.exit_code, result.output)
                self.assertIn("Usage:", result.output)

    def test_tc_build_returns_nonzero_when_compiler_reports_errors(self) -> None:
        build = {"error_count": 2, "errors": []}
        with patch("tc_template.cli.build_plc_project", return_value=build):
            result = self.runner.invoke(main, ["tc", "build"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("2 error(s)", result.output)

    def test_tc_check_returns_nonzero_when_compile_check_fails(self) -> None:
        with patch("tc_template.cli.check_all_objects", return_value={"error_count": 1}):
            result = self.runner.invoke(main, ["tc", "check"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("1 error(s)", result.output)

    def test_plc_build_returns_nonzero_for_failed_project(self) -> None:
        build = {"failedProjects": 1, "errorCount": 0, "errors": []}
        with patch("tc_template._ps_bridge.com_build", return_value=build):
            result = self.runner.invoke(main, ["plc", "build"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("PLC build failed", result.output)

    def test_plc_write_returns_nonzero_when_com_write_fails(self) -> None:
        with patch("tc_template._ps_bridge.com_write_pou", side_effect=RuntimeError("write failed")):
            result = self.runner.invoke(main, ["plc", "write", "MAIN", "x := 1;"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("write failed", result.output)

    def test_activate_returns_nonzero_when_runtime_operation_fails(self) -> None:
        with patch("tc_template._ps_bridge.com_activate", side_effect=RuntimeError("activate failed")):
            result = self.runner.invoke(main, ["tc", "activate"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("activate failed", result.output)

    def test_fblib_validate_returns_nonzero_for_findings(self) -> None:
        validation = {
            "summary": {"total": 1},
            "findings": [{"rule": "R1", "object": "FB_Test", "message": "bad"}],
        }
        with patch("tc_template.fblib.validate_fb", return_value=validation):
            result = self.runner.invoke(main, ["fblib", "validate", "test"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("FB validation failed", result.output)

    def test_fblib_add_returns_nonzero_for_partial_library_install(self) -> None:
        partial = {
            "fb": "FB_Test",
            "companions": [],
            "libraries": [],
            "library_warnings": ["Tc2_Test: unavailable"],
            "status": "partial",
        }
        with patch("tc_template.fblib.add_fb", return_value=partial):
            result = self.runner.invoke(main, ["fblib", "add", "test"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("only partially added", result.output)


if __name__ == "__main__":
    unittest.main()
