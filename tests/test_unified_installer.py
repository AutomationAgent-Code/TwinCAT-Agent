from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "packaging" / "installer"
if str(INSTALLER) not in sys.path:
    sys.path.insert(0, str(INSTALLER))

import installer_common as common  # noqa: E402


class UnifiedInstallerTests(unittest.TestCase):
    def test_finds_xae_shell_in_either_supported_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_shell = root / "ProgramFiles" / "TcXaeShell.exe"
            classic_shell = root / "TwinCAT4024" / "TcXaeShell.exe"
            package_shell.parent.mkdir(parents=True)
            classic_shell.parent.mkdir(parents=True)
            classic_shell.touch()
            self.assertEqual(
                common.find_xae_shell([package_shell, classic_shell]), classic_shell
            )
            package_shell.touch()
            self.assertEqual(
                common.find_xae_shell([package_shell, classic_shell]), package_shell
            )

    def test_standalone_shell_precedes_component_root_fallbacks(self) -> None:
        classic_4024 = common.xae_shell_candidates("3.1.4024.67")
        package_4026 = common.xae_shell_candidates("3.1.4026.18")
        self.assertIn("Program Files", str(classic_4024[0]))
        self.assertIn("Program Files", str(package_4026[0]))

    def test_shell_candidates_include_4024_custom_fallback(self) -> None:
        rendered = "\n".join(str(path) for path in common.xae_shell_candidates())
        self.assertIn("Components\\Base\\TcXaeShell", rendered)

    def test_detects_package_manager_version_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml = root / "SDK" / "TwinCATVersion.xml"
            xml.parent.mkdir(parents=True)
            xml.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<configuration><appSettings>
<add key="major" value="3"/><add key="minor" value="1"/>
<add key="build" value="4026"/><add key="revision" value="18"/>
</appSettings></configuration>""",
                encoding="utf-8",
            )
            self.assertEqual(common.detect_twincat_version([root]), "3.1.4026.18")

    def test_support_matrix_is_4024_and_4026(self) -> None:
        self.assertTrue(common.is_supported_twincat("3.1.4024.67"))
        self.assertTrue(common.is_supported_twincat("3.1.4026.18"))
        self.assertFalse(common.is_supported_twincat("3.1.4022.36"))
        self.assertFalse(common.is_supported_twincat(None))

    def test_install_options_default_to_legacy_embedded_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(common.read_install_options(root)["embed_xae"])
            (root / "install_options.json").write_text(
                '{"embed_xae": false}', encoding="utf-8"
            )
            self.assertFalse(common.read_install_options(root)["embed_xae"])

    def test_setup_gates_xae_changes_behind_embed_option(self) -> None:
        source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        self.assertIn("embed_xae: bool = True", source)
        self.assertIn("if not skip_system and embed_xae:", source)
        self.assertIn("嵌入 TwinCAT XAE", source)
        self.assertIn("embed_xae=self.embed_xae", source)
        self.assertIn('"xae_shell": str(shell) if shell else None', source)
        self.assertIn('"--install-extension-native"', source)
        self.assertIn('str(xae_shell_root(shell))', source)

    def test_setup_requires_and_records_agreement_before_install(self) -> None:
        agreement = (INSTALLER / "AGREEMENT.txt").read_text(encoding="utf-8-sig")
        setup = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        build = (ROOT / "scripts" / "build_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("TwinCAT Agent 软件使用协议与数据说明", agreement)
        self.assertIn("AGREEMENT_FILE = \"AGREEMENT.txt\"", setup)
        self.assertIn("agreement_accepted: bool = False", setup)
        self.assertIn("if not agreement_accepted:", setup)
        self.assertIn('"agreement_accepted": bool(agreement_accepted)', setup)
        self.assertIn('"agreement_accepted_at": datetime.now(timezone.utc).isoformat()', setup)
        self.assertIn("self.agreement = BooleanVar(value=False)", setup)
        self.assertIn("我已阅读并同意《TwinCAT Agent 软件使用协议与数据说明》", setup)
        self.assertIn("self._sync_start_button()", setup)
        self.assertIn('--add-data "$Agreement;assets"', build)

    def test_launcher_uses_browser_for_backend_only_install(self) -> None:
        source = (INSTALLER / "launcher.py").read_text(encoding="utf-8-sig")
        self.assertIn('options.get("embed_xae", True)', source)
        self.assertIn('webbrowser.open(UI_URL)', source)
        self.assertIn('options.get("xae_shell")', source)

    def test_launcher_keeps_tray_service_controller(self) -> None:
        launcher = (INSTALLER / "launcher.py").read_text(encoding="utf-8-sig")
        setup = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        build = (ROOT / "scripts" / "build_installer.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("def run_tray", launcher)
        self.assertIn("pystray.Icon", launcher)
        self.assertIn('MenuItem("启动服务"', launcher)
        self.assertIn('MenuItem("停止服务"', launcher)
        self.assertIn('MenuItem("重启服务"', launcher)
        self.assertIn('"tray_enabled": True', setup)
        self.assertIn("stop_installed_tray(target)", setup)
        self.assertNotIn('existing_launcher), "--shutdown"', setup)
        self.assertIn('--collect-all", "pystray"', build)

    def test_uninstaller_tracks_existing_extension_separately(self) -> None:
        setup_source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        uninstall_source = (INSTALLER / "uninstaller.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"extension_installed": extension_present', setup_source)
        self.assertIn('options.get("extension_installed"', uninstall_source)

    def test_native_installer_keeps_4024_fallbacks_without_powershell(self) -> None:
        common_source = (INSTALLER / "installer_common.py").read_text(encoding="utf-8-sig")
        setup_source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        uninstall_source = (INSTALLER / "uninstaller.py").read_text(encoding="utf-8-sig")
        self.assertIn(r"C:\TwinCAT\3.1", common_source)
        self.assertIn("TWINCAT3DIR", common_source)
        self.assertNotIn('run_elevated("powershell.exe"', common_source)
        self.assertNotIn('"powershell.exe"', setup_source)
        self.assertNotIn('"powershell.exe"', uninstall_source)

    def test_native_extension_install_and_remove_need_no_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shell_root = root / "TcXaeShell"
            ide = shell_root / "Common7" / "IDE"
            ide.mkdir(parents=True)
            (ide / "TcXaeShell.exe").touch()
            source = root / "source"
            source.mkdir()
            (source / "TwinCATAgent.Xae.dll").write_bytes(b"agent")
            (source / "TwinCATAgent.Xae.pkgdef").write_text("pkgdef", encoding="utf-8")
            (source / "extension.vsixmanifest").write_text("manifest", encoding="utf-8")

            copied = common.install_extension_native(source, shell_root)
            destination = ide / "Extensions" / "TwinCAT Agent"
            self.assertEqual(3, copied)
            self.assertTrue((destination / "TwinCATAgent.Xae.dll").is_file())
            (destination / "TcCoAgent.dll").write_bytes(b"old agent")
            (destination / "TcCoAgent.pkgdef").write_text("old pkgdef", encoding="utf-8")
            (destination / "TwinCATAgent.dll").write_bytes(b"failed renamed agent")
            (destination / "TwinCATAgent.pkgdef").write_text("failed renamed", encoding="utf-8")
            # Upgrade must replace the managed DLL without deleting the whole
            # directory first (Program Files can contain protected old files).
            self.assertEqual(3, common.install_extension_native(source, shell_root))
            self.assertEqual(b"agent", (destination / "TwinCATAgent.Xae.dll").read_bytes())
            self.assertFalse((destination / "TcCoAgent.dll").exists())
            self.assertFalse((destination / "TcCoAgent.pkgdef").exists())
            self.assertFalse((destination / "TwinCATAgent.dll").exists())
            self.assertFalse((destination / "TwinCATAgent.pkgdef").exists())
            self.assertTrue(common.remove_extension_native(shell_root))
            self.assertFalse(destination.exists())

    def test_native_extension_install_restores_previous_directory_on_refresh_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shell_root = root / "TcXaeShell"
            ide = shell_root / "Common7" / "IDE"
            ide.mkdir(parents=True)
            (ide / "TcXaeShell.exe").touch()
            source = root / "source"
            source.mkdir()
            (source / "TwinCATAgent.Xae.dll").write_bytes(b"new agent")
            (source / "TwinCATAgent.Xae.pkgdef").write_text("new pkgdef", encoding="utf-8")
            (source / "extension.vsixmanifest").write_text("new manifest", encoding="utf-8")
            destination = ide / "Extensions" / "TwinCAT Agent"
            destination.mkdir(parents=True)
            (destination / "TwinCATAgent.Xae.dll").write_bytes(b"old agent")
            (destination / "TwinCATAgent.Xae.pkgdef").write_text("old pkgdef", encoding="utf-8")
            (destination / "extension.vsixmanifest").write_text("old manifest", encoding="utf-8")
            (destination / "old-unrelated.txt").write_text("preserved", encoding="utf-8")

            with patch.object(common, "refresh_xae_package_cache", side_effect=RuntimeError("setup failed")):
                with self.assertRaisesRegex(RuntimeError, "setup failed"):
                    common.install_extension_native(source, shell_root, refresh_cache=True)

            self.assertEqual(b"old agent", (destination / "TwinCATAgent.Xae.dll").read_bytes())
            self.assertEqual("old pkgdef", (destination / "TwinCATAgent.Xae.pkgdef").read_text(encoding="utf-8"))
            self.assertEqual("preserved", (destination / "old-unrelated.txt").read_text(encoding="utf-8"))

    def test_tcxaeshell_pkgdef_has_an_explicit_new_assembly_codebase(self) -> None:
        pkgdef_path = ROOT / "tc_agent_vsix" / "deploy.TwinCATAgent.Xae.pkgdef"
        pkgdef_bytes = pkgdef_path.read_bytes()
        self.assertTrue(all(byte < 128 for byte in pkgdef_bytes))
        pkgdef = pkgdef_bytes.decode("ascii")
        self.assertIn('"Class"="TwinCATAgent.Xae.TwinCATAgentPackage"', pkgdef)
        self.assertIn('"Assembly"="TwinCATAgent.Xae, Version=0.1.0.0', pkgdef)
        self.assertIn('"CodeBase"="$PackageFolder$\\TwinCATAgent.Xae.dll"', pkgdef)

    def test_xae_rename_preserves_upgrade_identity_and_all_stable_guids(self) -> None:
        project = (ROOT / "tc_agent_vsix" / "TwinCATAgent.Xae.csproj").read_text(encoding="utf-8-sig")
        pkgdef = (ROOT / "tc_agent_vsix" / "deploy.TwinCATAgent.Xae.pkgdef").read_text(encoding="ascii")
        vsct = (ROOT / "tc_agent_vsix" / "TwinCATAgentPackage.vsct").read_text(encoding="utf-8-sig")
        manifests = [
            (ROOT / "tc_agent_vsix" / name).read_text(encoding="utf-8-sig")
            for name in ("source.extension.vsixmanifest", "source17.extension.vsixmanifest", "deploy.extension.vsixmanifest")
        ]
        self.assertIn("<AssemblyName>TwinCATAgent.Xae</AssemblyName>", project)
        self.assertIn("<RootNamespace>TwinCATAgent.Xae</RootNamespace>", project)
        self.assertIn("<ProjectGuid>{7E3A9C41-2D5B-4F8A-9C6E-1B0D2A5C8E30}</ProjectGuid>", project)
        self.assertIn("{6b6f9d1e-7c4e-4a6b-9e2d-1f0a5c8b3e21}", pkgdef)
        self.assertIn("{a1c2e3f4-5b6d-4e7f-8a9b-0c1d2e3f4a5b}", pkgdef)
        self.assertIn("{f0e1d2c3-b4a5-4968-8776-5a4b3c2d1e0f}", vsct)
        for manifest in manifests:
            self.assertIn("TcCoAgent.6b6f9d1e-7c4e-4a6b-9e2d-1f0a5c8b3e21", manifest)

    def test_installer_registers_the_extension_with_the_detected_xae_shell(self) -> None:
        common_source = (INSTALLER / "installer_common.py").read_text(encoding="utf-8-sig")
        setup_source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        portable_source = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("def refresh_xae_package_cache", common_source)
        self.assertIn('[str(shell), "/setup"]', common_source)
        self.assertNotIn("def register_extension_vsix", common_source)
        self.assertIn('"--refresh-cache"', setup_source)
        self.assertIn("refresh_cache=\"--refresh-cache\"", setup_source)
        self.assertIn("TwinCAT-Agent-XAE.vsix", portable_source)
        self.assertIn("$AgentVersion", portable_source)
        self.assertIn("TwinCATAgent.Xae.vsix", portable_source)
        self.assertIn("TcCoAgent.dll", common_source)
        self.assertIn("TwinCATAgent.Xae.dll", common_source)
        self.assertIn("TwinCAT-CoAgent/ChatVs.dll", common_source)
        self.assertNotIn("Compress-Archive -Path (Join-Path $extDst '*')", portable_source)

    def test_standalone_extension_script_uses_native_xae_setup(self) -> None:
        source = (ROOT / "packaging" / "portable" / "Install-Extension.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('$ShellExe = Join-Path $Shell "Common7\\IDE\\TcXaeShell.exe"', source)
        self.assertIn('Start-Process -FilePath $ShellExe -ArgumentList "/setup" -Wait', source)
        self.assertIn("同一个管理员流程执行 TcXaeShell.exe /setup", source)
        self.assertNotIn("VSIXInstaller", source)
        self.assertNotIn("Remove-Item $Dest -Recurse -Force", source)

    def test_setup_window_uses_the_branded_title_bar_icon(self) -> None:
        setup_source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        self.assertIn('self.root.iconbitmap(default=str(resource_path("assets", "twincat-agent.ico")))', setup_source)

    def test_upgrade_stops_only_its_own_locked_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "runtime" / "python" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(common, "_listener_pids", side_effect=[{4321}, set()]), \
                 patch.object(common, "_process_image", return_value=python), \
                 patch.object(common, "_terminate_process", return_value=True) as stop:
                self.assertTrue(common.stop_installed_backend(root))
            stop.assert_called_once_with(4321, timeout=6.0)

    def test_upgrade_accepts_backend_from_legacy_product_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            current = base / "TwinCAT Agent"
            legacy = base / "TwinCATAgent"
            legacy_python = legacy / "runtime" / "python" / "python.exe"
            legacy_python.parent.mkdir(parents=True)
            legacy_python.touch()
            with patch.object(common, "legacy_install_dirs", return_value=[legacy]), \
                 patch.object(common, "_listener_pids", side_effect=[{2468}, set()]), \
                 patch.object(common, "_process_image", return_value=legacy_python), \
                 patch.object(common, "_terminate_process", return_value=True) as stop:
                self.assertTrue(common.stop_installed_backend(current))
            stop.assert_called_once_with(2468, timeout=6.0)

    def test_upgrade_never_kills_unrelated_port_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "runtime" / "python" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(common, "_listener_pids", return_value={9876}), \
                 patch.object(common, "_process_image", return_value=Path(r"C:\Other\python.exe")), \
                 patch.object(common, "_terminate_process") as stop, \
                 patch.object(common, "backend_is_running", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "其他程序占用"):
                    common.stop_installed_backend(root)
            stop.assert_not_called()

    def test_upgrade_never_treats_kernel_pid_zero_as_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(common, "_listener_pids", return_value={0}), \
                 patch.object(common, "_backend_info", return_value={
                     "product": "TwinCAT Agent", "pid": 0,
                     "project_dir": str(root / "app"),
                 }), \
                 patch.object(common, "backend_is_running", return_value=True), \
                 patch.object(common, "_terminate_process") as stop:
                with self.assertRaisesRegex(RuntimeError, "其他程序占用"):
                    common.stop_installed_backend(root)
            stop.assert_not_called()

    def test_upgrade_accepts_system_python_backend_claiming_current_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "_backend.log"
            log.write_text(
                f"[tc-agent] serving (self-built brain, project: {root / 'app'})\n",
                encoding="utf-8",
            )
            with patch.object(common, "_listener_pids", side_effect=[{5432}, set()]), \
                 patch.object(common, "_process_image",
                              return_value=Path(r"C:\Python\python.exe")), \
                 patch.object(common, "_backend_info", return_value={}), \
                 patch.object(common, "backend_is_running", return_value=True), \
                 patch.object(common, "_terminate_process", return_value=True) as stop:
                self.assertTrue(common.stop_installed_backend(root))
            stop.assert_called_once_with(5432, timeout=6.0)

    def test_upgrade_accepts_current_install_from_identity_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "runtime" / "python" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(common, "_listener_pids", side_effect=[{6543}, set()]), \
                 patch.object(common, "_process_image", return_value=Path(r"C:\Python\python.exe")), \
                 patch.object(common, "_backend_info", return_value={
                     "product": "TwinCAT Agent", "pid": 6543,
                     "project_dir": str(root / "app"),
                 }), \
                 patch.object(common, "_terminate_process", return_value=True) as stop:
                self.assertTrue(common.stop_installed_backend(root))
            stop.assert_called_once_with(6543, timeout=6.0)

    def test_setup_and_uninstaller_stop_backend_before_removal(self) -> None:
        setup_source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        uninstall_source = (INSTALLER / "uninstaller.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertLess(
            setup_source.index("stop_installed_backend(target)"),
            setup_source.index("shutil.rmtree(target)"),
        )
        self.assertLess(
            uninstall_source.index("stop_installed_backend(target)"),
            uninstall_source.index("shutil.rmtree(target)"),
        )
        self.assertLess(
            setup_source.index("stop_installed_backend(target)"),
            setup_source.index("stop_installed_tray(target)"),
        )
        self.assertLess(
            uninstall_source.index("stop_installed_backend(target)"),
            uninstall_source.index("stop_installed_tray(target)"),
        )
        self.assertLess(
            setup_source.index("stop_installed_tray(target)"),
            setup_source.index("shutil.rmtree(target)"),
        )
        self.assertLess(
            uninstall_source.index("stop_installed_tray(target)"),
            uninstall_source.index("shutil.rmtree(target)"),
        )

    def test_setup_test_mode_does_not_touch_real_installations(self) -> None:
        source = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        stop_call = source.index("stop_installed_backend(target)")
        stop_guard = source.rindex("if not skip_system:", 0, stop_call)
        stop_else = source.index("else:", stop_call)
        self.assertLess(stop_guard, stop_call)
        self.assertLess(stop_call, stop_else)

        legacy_loop = source.index("for legacy_root in legacy_install_dirs()")
        legacy_guard = source.rindex("if not skip_system:", 0, legacy_loop)
        self.assertLess(legacy_guard, legacy_loop)

    def test_build_outputs_do_not_leave_intermediates_in_dist(self) -> None:
        portable = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        installer = (ROOT / "scripts" / "build_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"build\\portable-cache"', portable)
        self.assertIn("$temporaryOutput", installer)
        self.assertIn("$PortableDir", installer)

    def test_dynamic_ads_bridge_is_built_for_portable_and_local_updates(self) -> None:
        portable = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        updater = (ROOT / "scripts" / "Update-LocalInstallation.ps1").read_text(
            encoding="utf-8-sig"
        )
        bridge = (ROOT / "scripts" / "build_ads_dynamic_bridge.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("build_ads_dynamic_bridge.ps1", portable)
        self.assertIn("build_ads_dynamic_bridge.ps1", updater)
        self.assertIn("TcAdsDynamicBridge.exe", bridge)
        self.assertIn("TwinCAT.Ads.dll", bridge)

    def test_setup_filename_and_metadata_share_one_version_source(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+(?:\.\d+)?$")
        installer = (ROOT / "scripts" / "build_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        version_info = (INSTALLER / "version_info.txt").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('$FinalName = "TwinCAT-Agent-Setup-v$AppVersion.exe"', installer)
        self.assertIn("__VERSION_TUPLE__", version_info)
        self.assertIn("__SETUP_FILENAME__", version_info)

    def test_setup_window_displays_the_embedded_app_version(self) -> None:
        setup = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        self.assertIn('"payload", "TwinCAT-Agent-Portable", "app", "VERSION"', setup)
        self.assertIn('APP_TITLE = f"TwinCAT Agent 安装程序 v{APP_VERSION}"', setup)
        self.assertIn('text=f"TwinCAT Agent  ·  v{APP_VERSION}"', setup)

    def test_customer_package_excludes_local_conversation_database_and_caches(self) -> None:
        portable = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        installer = (ROOT / "scripts" / "build_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        setup = (INSTALLER / "setup.py").read_text(encoding="utf-8-sig")
        self.assertIn('agent.db "agent.db-*"', portable)
        self.assertIn("客户程序包含本地私有数据", portable)
        self.assertIn("__pycache__", portable)
        self.assertIn("agent\\.db", installer)
        self.assertIn('"app/tc_agent/agent.db"', setup)

    def test_local_updater_stops_verified_agent_port_owner(self) -> None:
        updater = (ROOT / "scripts" / "Update-LocalInstallation.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Get-NetTCPConnection -LocalPort 8765,8766", updater)
        self.assertIn("$owner.CommandLine -like '*tc_agent.backend*'", updater)
        self.assertNotIn("Stop-Process -Id $ownerPid", updater)


if __name__ == "__main__":
    unittest.main()
