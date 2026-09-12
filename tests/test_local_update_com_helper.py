"""Exercise updater planning/copying only against temporary installations."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'Update-LocalInstallation.ps1'
PS = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(not PS, reason='Windows PowerShell required')


@pytest.mark.parametrize('kind', ['source', 'packaged', 'missing', 'mixed'])
def test_update_plans_matching_com_helper_before_mutation(tmp_path, kind):
    install = tmp_path / 'installed'
    package = tmp_path / 'package'
    for root in (install, package):
        (root / 'app' / 'tc_template').mkdir(parents=True)
        (root / 'app' / 'tc_agent').mkdir()
    (install / 'runtime' / 'com32').mkdir(parents=True)
    (install / 'runtime' / 'com32' / 'python.exe').touch()
    (install / 'Start-Backend.vbs').touch()
    if kind in ('source', 'mixed'):
        (package / 'app' / 'tc_template' / '_native_worker.py').write_text('# fixture')
    if kind in ('missing', 'mixed'):
        (package / 'app' / 'tc_template' / '_native_worker.pyc').write_bytes(b'wrong-version')
    if kind == 'packaged':
        helper = package / 'runtime' / 'com32' / 'app' / 'tc_template'
        helper.mkdir(parents=True)
        (helper / '_native_worker.pyc').write_bytes(b'correct-version')
    proc = subprocess.run([
        PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
        '-InstallDir', str(install), '-PackageDir', str(package), '-DryRun',
    ], capture_output=True, text=True, errors='replace', timeout=20)
    if kind in ('source', 'packaged'):
        assert proc.returncode == 0, proc.stderr
        assert 'com32-app' in proc.stdout
    else:
        assert proc.returncode != 0
        assert 'architecture-matched' in proc.stderr
    assert not (install / '.updates').exists()
    assert not (install / 'runtime' / 'com32' / 'app').exists()


def test_copy_preserves_release_bytecode_and_excludes_user_data(tmp_path):
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    source.mkdir()
    dest.mkdir()
    (dest / 'module.py').write_text('# stale development source')
    (dest / 'user_extension.py').write_text('# unrelated local file')
    (source / '_script_bundle.pyc').write_bytes(b'bundled-resources')
    (dest / 'TcCom.ps1').write_text('# stale fallback')
    (dest / 'custom.ps1').write_text('# unrelated script')
    (source / 'module.pyc').write_bytes(b'interpreter-specific-bytecode')
    (source / 'config.json').write_text('private-setting')
    (source / 'agent.db').write_text('private-history')
    (source / '__pycache__').mkdir()
    (source / '__pycache__' / 'disposable.pyc').touch()
    # Extract only Copy-Tree; never execute updater top-level process control.
    command = """param($scriptPath, $sourcePath, $destPath)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Updater syntax error' }
$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Copy-Tree' }, $true)
Invoke-Expression $function.Extent.Text
Copy-Tree $sourcePath $destPath -ProgramOnly
"""
    runner = tmp_path / 'copy-test.ps1'
    runner.write_text(command, encoding='utf-8-sig')
    proc = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                           str(runner), str(SCRIPT), str(source), str(dest)],
                          capture_output=True, text=True, errors='replace', timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert (dest / 'module.pyc').read_bytes() == b'interpreter-specific-bytecode'
    assert not (dest / 'module.py').exists()
    assert (dest / 'user_extension.py').exists()
    assert not (dest / 'TcCom.ps1').exists()
    assert (dest / 'custom.ps1').exists()
    assert not (dest / 'config.json').exists()
    assert not (dest / 'agent.db').exists()
    assert not (dest / '__pycache__').exists()
