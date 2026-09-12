from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_agent import agent_core, config
from tc_template.hooks import run_hooks
from tc_template.models import Hook, TemplateMetadata, TemplateValidationError
from tc_template.repository import add_template, get_template, get_template_source_dir
from tc_template.scaffold import _contained_destination


class PermissionSafetyTests(unittest.TestCase):
    def test_unknown_permission_mode_fails_closed(self) -> None:
        self.assertEqual("default", config.sdk_permission_mode("unexpected"))

    def test_migration_defaults_to_ask(self) -> None:
        migrated = config._migrate({})
        self.assertEqual("ask", migrated["perm_mode"])
        self.assertTrue(migrated["quality_gate_enabled"])

    def test_raw_target_requires_strict_six_part_netid(self) -> None:
        with patch.object(agent_core, "resolve_target", side_effect=ValueError("missing")), \
                patch.object(agent_core, "ps_com") as ps_com:
            with self.assertRaises(ValueError):
                agent_core._target_set({"target": "controller.example"})
            with self.assertRaises(ValueError):
                agent_core._target_set({"target": "1.2.3.4.5.999"})
            ps_com.assert_not_called()

    def test_valid_raw_netid_can_be_selected(self) -> None:
        with patch.object(agent_core, "resolve_target", side_effect=ValueError("missing")), \
                patch.object(agent_core, "ps_com", return_value={"ok": True}) as ps_com:
            result = agent_core._target_set({"target": "1.2.3.4.5.6"})
            ps_com.assert_called_once_with("target-set", netid="1.2.3.4.5.6")
            self.assertEqual("raw_netid", result["matched_by"])


class TemplatePathSafetyTests(unittest.TestCase):
    def test_repository_rejects_template_name_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(TemplateValidationError):
                get_template("../outside", repo_dir=td)

    def test_repository_rejects_escaping_template_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "source"
            src.mkdir()
            meta = TemplateMetadata(name="safe-name", template_dir="../outside")
            with self.assertRaises(TemplateValidationError):
                add_template(src, meta, repo_dir=root / "repo")

    def test_source_dir_rejects_untrusted_metadata_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            template = root / "safe-name"
            template.mkdir()
            (template / "template.yaml").write_text(
                "name: safe-name\ntemplate_dir: ../outside\n", encoding="utf-8"
            )
            with self.assertRaises(TemplateValidationError):
                get_template_source_dir("safe-name", repo_dir=root)

    def test_generated_file_name_cannot_escape_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                _contained_destination(Path(td), "../payload.ps1")


class HookSafetyTests(unittest.TestCase):
    def test_hook_execution_is_disabled_by_default(self) -> None:
        with patch("tc_template.hooks.subprocess.run") as run:
            with self.assertRaises(PermissionError):
                run_hooks([Hook(cmd="tool --flag")], {})
            run.assert_not_called()

    def test_trusted_hook_uses_argv_without_shell(self) -> None:
        with patch("tc_template.hooks.subprocess.run") as run:
            run_hooks([Hook(cmd="tool --flag")], {}, allow_execution=True)
            argv = run.call_args.args[0]
            self.assertEqual(["tool", "--flag"], argv)
            self.assertFalse(run.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
