import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def invoke(tmp_path):
    if not shutil.which('powershell'):
        pytest.skip('Windows PowerShell is required')
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    functions = 'function Read-TcHmiFrameworkPackage {' + source.split('function Read-TcHmiFrameworkPackage {', 1)[1].split('function Get-TcHmiFrameworkPackages {', 1)[0]
    script = tmp_path / 'inspect.ps1'
    script.write_text('param([string]$InputFile)\n$ErrorActionPreference="Stop"\n' + functions + '''
try {
    $a = [IO.File]::ReadAllText($InputFile) | ConvertFrom-Json
    $r = if ($a.mode -eq 'archives') { Get-TcHmiPackageArchives $a.folder $a.id $a.version }
         else { Read-TcHmiFrameworkPackage $a.package }
    @{ok=$true;data=$r} | ConvertTo-Json -Depth 30 -Compress
} catch { @{ok=$false;error=$_.Exception.Message} | ConvertTo-Json -Compress }
''', encoding='utf-8-sig')
    def call(**args):
        file = tmp_path / 'args.json'
        file.write_text(json.dumps(args), encoding='utf-8')
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File', str(script), '-InputFile', str(file)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return call


def package(tmp_path, types=('Control',), package_id='Test.Package', missing_description=False, namespace='http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd'):
    path = tmp_path / 'Test.Package.1.2.3.nupkg'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('Test.nuspec', f'<package xmlns="{namespace}"><metadata><id>{package_id}</id><version>1.2.3</version></metadata></package>')
        modules = []
        for i, kind in enumerate(types):
            module = {'type': kind}
            if kind in ('Control', 'Function'):
                module.update(basePath=f'Item{i}', descriptionFile='Description.json')
                if not missing_description:
                    z.writestr(f'runtimes/native1.12-tchmi/Item{i}/Description.json', json.dumps({'name': f'Item{i}', 'namespace': 'Test'}))
            modules.append(module)
        z.writestr('runtimes/native1.12-tchmi/Manifest.json', json.dumps({'apiVersion': 1, 'modules': modules}))
    return path


@pytest.mark.parametrize('types,kind', [
    (['Control'], 'controls'), (['Function', 'Package'], 'functions'),
    (['Control', 'Function'], 'mixed'), (['Resource', 'Language'], 'resources'),
])
def test_inspects_module_kinds_without_requiring_controls(tmp_path, invoke, types, kind):
    result = invoke(package=str(package(tmp_path, types)))
    assert result['ok'], result
    info = result['data']
    assert info['package_kind'] == kind
    assert info['control_count'] == types.count('Control')
    assert info['function_count'] == types.count('Function')
    assert info['install_performed'] is False and info['runtime_verified'] is False


def test_framework_identity_and_legacy_nuspec_namespace(tmp_path, invoke):
    result = invoke(package=str(package(tmp_path, package_id='Beckhoff.TwinCAT.HMI.Framework', namespace='http://schemas.microsoft.com/packaging/2012/06/nuspec.xsd')))
    assert result['ok'] and result['data']['package_kind'] == 'framework'


@pytest.mark.parametrize('kind', ['Control', 'Function'])
def test_missing_description_remains_failure(tmp_path, invoke, kind):
    result = invoke(package=str(package(tmp_path, [kind], missing_description=True)))
    assert not result['ok']
    assert 'description is missing' in result['error']


def test_empty_manifest_rejected(tmp_path, invoke):
    assert not invoke(package=str(package(tmp_path, [])))['ok']


def test_absent_archive_gives_discovery_hint(tmp_path, invoke):
    result = invoke(package=str(tmp_path / 'invented.nupkg'))
    assert not result['ok'] and 'package_path' in result['error']


def test_inventory_only_returns_existing_exact_archive(tmp_path, invoke):
    archive = package(tmp_path)
    result = invoke(mode='archives', folder=str(tmp_path), id='Test.Package', version='1.2.3')['data']
    assert result['package_path'] == str(archive)
    assert result['package_exists'] is True
    assert result['archive_identity_verified'] is False
    wrong = invoke(mode='archives', folder=str(tmp_path), id='Test.Package', version='1.2.4')['data']
    assert wrong['package_path'] is None and wrong['package_exists'] is False
    assert wrong['package_candidates'] == [str(archive)]


def test_missing_archive_is_not_invented(tmp_path, invoke):
    result = invoke(mode='archives', folder=str(tmp_path), id='Test.Package', version='1.2.3')['data']
    assert result['archive_status'] == 'missing' and result['package_path'] is None


def test_inspection_change_does_not_broaden_installation():
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    body = source.split('function Install-TcHmiFrameworkPackage {', 1)[1].split('\nfunction ', 1)[0]
    assert body.index('@($packageInfo.controls).Count -eq 0') < body.index('Get-TcHmiProjectObject')


def test_model_context_retains_real_archive_paths():
    from tc_agent.agent_core import tool_result_for_context
    packages = [{'id': f'Pkg{i}', 'version': f'1.0.{i}', 'folder': 'ignored' * 100,
                 'package_path': f'C:/Project/Packages/Pkg{i}.1.0.{i}/Pkg{i}.1.0.{i}.nupkg',
                 'package_exists': True, 'archive_status': 'found'} for i in range(5)]
    out = tool_result_for_context('tc_hmi_framework_packages', {}, {'status': 'ok', 'packages': packages})
    assert [p['package_path'] for p in out['packages']] == [p['package_path'] for p in packages]
    assert out['packages_truncated'] is False
