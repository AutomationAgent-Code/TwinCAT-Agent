import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tc_agent.agent_core import _hmi_read_result_for_context
from tc_agent.execution_policy import ReadPolicy
from tc_template._ps_bridge import com_hmi_read


def page(count=60, total=78, content=False):
    return {'status': 'read', 'file': 'Desktop.view', 'content_included': content,
            'controls': [{'id': f'C{i}', 'type': 'TcHmi.Controls.Beckhoff.TcHmiText'} for i in range(count)],
            'control_offset': 0, 'control_count': total, 'total_control_count': total,
            'returned_control_count': count, 'controls_truncated': count < total,
            'bindings': [], 'bindings_truncated': False,
            'content_chars': 0, 'truncated': False}


def test_reported_60_to_80_and_catalog_to_source_regression():
    policy = ReadPolicy()
    args = {'file': 'Desktop.view', 'include_content': False, 'max_controls': 60}
    policy.record('tc_hmi_read', args, ok=True, result=page())
    assert policy.duplicate('tc_hmi_read', args)
    assert not policy.duplicate('tc_hmi_read', {**args, 'max_controls': 80})
    assert not policy.duplicate('tc_hmi_read', {**args, 'include_content': True})
    assert not policy.duplicate('tc_hmi_read', {**args, 'control_offset': 60})
    policy.record('tc_hmi_read', {**args, 'max_controls': 80}, ok=True, result=page(78))
    assert policy.duplicate('tc_hmi_read', {**args, 'max_controls': 1000})


def test_source_expansion_and_paging_only_until_complete():
    policy = ReadPolicy()
    args = {'file': 'Desktop.view', 'include_content': True, 'max_chars': 1000}
    result = {**page(0, 0), 'content_included': True, 'content_chars': 1000, 'truncated': True}
    policy.record('tc_hmi_read', args, ok=True, result=result)
    assert policy.duplicate('tc_hmi_read', args)
    assert not policy.duplicate('tc_hmi_read', {**args, 'max_chars': 3000})
    assert not policy.duplicate('tc_hmi_read', {**args, 'content_offset': 1000})
    assert not policy.duplicate('tc_hmi_read', {**args, 'control_id': 'Button'})


def test_saved_file_change_and_missing_fingerprint_invalidate_dedupe(tmp_path):
    path = tmp_path / 'Desktop.view'
    path.write_text('old')
    stat = path.stat()
    result = {**page(1, 1), 'full_path': str(path), 'source_size': stat.st_size,
              'source_mtime_ticks': stat.st_mtime_ns // 100 + 621355968000000000,
              'source_consistent': True}
    args = {'file': 'Desktop.view', 'include_content': False}
    policy = ReadPolicy()
    policy.record('tc_hmi_read', args, ok=True, result=result)
    assert policy.duplicate('tc_hmi_read', args)
    path.write_text('new longer saved content')
    assert not policy.duplicate('tc_hmi_read', args)
    policy.record('tc_hmi_read', args, ok=True, result=result)
    assert not policy.duplicate('tc_hmi_read', args)
    policy.record('tc_hmi_read', args, ok=True, result={**result, 'source_consistent': False})
    assert not policy.duplicate('tc_hmi_read', args)


def test_context_complete_catalog_no_fixed_24_cutoff():
    result = _hmi_read_result_for_context({'include_content': False}, page(40, 78))
    assert len(result['controls']) == 40
    assert result['next_control_offset'] == 40
    assert result['returned_control_count'] == 40
    assert len(json.dumps(result, ensure_ascii=False)) <= 6000


def test_context_cursor_tracks_delivered_not_tool_returned_controls():
    result = page(100, 150)
    context = _hmi_read_result_for_context({'include_content': False}, result)
    assert 0 < context['returned_control_count'] < 100
    assert context['next_control_offset'] == len(context['controls'])
    policy = ReadPolicy()
    args = {'file': 'Desktop.view', 'include_content': False, 'max_controls': 100}
    policy.record('tc_hmi_read', args, ok=True, result=context)
    assert not policy.duplicate('tc_hmi_read', {**args, 'control_offset': context['next_control_offset']})


def test_source_page_is_contiguous_utf16_and_survives_recompaction():
    text = '<div>中文😀&quot;\\\n' * 3000
    result = {**page(0, 0, True), 'content': text, 'content_chars': len(text.encode('utf-16-le')) // 2,
              'content_offset': 120, 'truncated': False}
    context = _hmi_read_result_for_context({'include_content': True}, result)
    assert text.startswith(context['content']) and len(context['content']) > 1200
    assert context['next_content_offset'] == 120 + len(context['content'].encode('utf-16-le')) // 2
    assert context['truncated'] and len(json.dumps(context, ensure_ascii=False)) < 6000
    again = _hmi_read_result_for_context({}, context)
    assert again['content'] == context['content']
    assert again['next_content_offset'] == context['next_content_offset']


def test_bridge_forwards_page_offsets():
    with patch('tc_template._ps_bridge.ps_com', return_value={}) as com:
        com_hmi_read('Desktop.view', control_offset=60, content_offset=4000)
    assert com.call_args.kwargs['control_offset'] == 60
    assert com.call_args.kwargs['content_offset'] == 4000


def test_cli_forwards_page_offsets():
    from click.testing import CliRunner
    from tc_template.cli import main
    with patch('tc_template._ps_bridge.com_hmi_read', return_value={'status': 'read'}) as read:
        result = CliRunner().invoke(main, ['hmi', 'read', 'Desktop.view', '--no-content',
                                          '--control-offset', '60', '--content-offset', '1200'])
    assert result.exit_code == 0, result.output
    assert read.call_args.kwargs['control_offset'] == 60
    assert read.call_args.kwargs['content_offset'] == 1200


def test_guard_message_survives_context_summary():
    result = {'status': 'duplicate_read_skipped', 'message': 'Continue from returned offsets.'}
    assert _hmi_read_result_for_context({}, result) == result


def ps_read(tmp_path, control_offset=0, content_offset=0, max_chars=30, control_id=''):
    if not shutil.which('powershell'):
        pytest.skip('Windows PowerShell required')
    script = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:HMI_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($d in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
 if ($d.Name -eq 'Read-TcHmiFile') { . ([scriptblock]::Create($d.Extent.Text)) }
}
function Get-TcHmiProjectObject { param($Dte,$ProjectName) [pscustomobject]@{Name='HMI'} }
function Get-TcHmiResolvedName { param($Project) 'HMI' }
function Get-TcHmiProjectPath { param($Project) $env:HMI_ROOT }
function Resolve-TcHmiFilePath { param($Root,$File,$Extensions) Join-Path $Root $File }
Read-TcHmiFile -Dte ([pscustomobject]@{}) -File 'Desktop.view' -MaxControls 60 `
 -ControlOffset ([int]$env:CONTROL_OFFSET) -ContentOffset ([int]$env:CONTENT_OFFSET) `
 -MaxChars ([int]$env:MAX_CHARS) -ControlId $env:CONTROL_ID | ConvertTo-Json -Depth 12 -Compress
'''
    env = dict(os.environ, HMI_SCRIPT=str(Path(__file__).resolve().parents[1] / 'tc_template/TcCom.ps1'),
               HMI_ROOT=str(tmp_path), CONTROL_OFFSET=str(control_offset), CONTENT_OFFSET=str(content_offset),
               MAX_CHARS=str(max_chars), CONTROL_ID=control_id)
    proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16-le')).decode()], capture_output=True, timeout=20, env=env)
    assert proc.returncode == 0, proc.stderr.decode(errors='replace')
    # Windows PowerShell writes escaped surrogate pairs safely through JSON.
    return json.loads(proc.stdout.decode('utf-8-sig'))


def test_real_ps_pages_complete_78_controls_and_stat_identity(tmp_path):
    source = '<div>' + ''.join(f'<div id="C{i}" data-tchmi-type="Text"></div>' for i in range(78)) + '</div>'
    path = tmp_path / 'Desktop.view'
    path.write_text(source, encoding='utf-8')
    first = ps_read(tmp_path)
    second = ps_read(tmp_path, control_offset=first['next_control_offset'], content_offset=first['next_content_offset'])
    assert len(first['controls']) == 60 and len(second['controls']) == 18
    assert second['next_control_offset'] is None and not second['controls_truncated']
    assert [c['id'] for c in first['controls'] + second['controls']] == [f'C{i}' for i in range(78)]
    assert first['content'] + second['content'] == source[:60]
    assert first['source_mtime_ticks'] == path.stat().st_mtime_ns // 100 + 621355968000000000
    assert first['source_consistent'] and first['dirty_unknown'] and not first['live_xae']
    focused = ps_read(tmp_path, control_offset=60, control_id='C77')
    assert [c['id'] for c in focused['controls']] == ['C77']
    assert focused['next_control_offset'] is None


def test_real_ps_surrogate_boundary_does_not_lose_source(tmp_path):
    source = '<div>😀abc</div>'
    (tmp_path / 'Desktop.view').write_text(source, encoding='utf-8')
    first = ps_read(tmp_path, max_chars=6)
    second = ps_read(tmp_path, content_offset=first['next_content_offset'], max_chars=100)
    assert first['content'] + second['content'] == source
