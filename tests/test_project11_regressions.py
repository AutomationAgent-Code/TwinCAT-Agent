import json
from pathlib import Path
from unittest.mock import patch

from tc_agent.recovery import make_recovery, resume_text, original_request, error_category
from tc_agent.completion_evidence import CompletionEvidence, plc_entry_evidence
from tc_agent.agent_core import run_tool


def test_recovery_unwraps_without_carrying_provider_fields():
    request = '创建HMI和PLC案例'
    messages = [{'role': 'assistant', 'text': '已经读取', 'reasoning_content': 'private-test',
                 'tool_calls': [{'name': 'plc_read', 'args': {'code': 'x' * 20000}}]}]
    for _ in range(8):
        record = make_recovery(messages, request, 'API 400 Unsupported model MiMo')
        request = resume_text(record, '继续')
    assert record['request'] == '创建HMI和PLC案例'
    assert len(request) < 2000
    assert 'private-test' not in json.dumps(record)
    assert json.loads(record['recent_context'])[0]['tool_names'] == ['plc_read']
    assert record['error_category'] == 'provider_configuration'


def test_recovery_does_not_strip_ordinary_user_request():
    assert original_request('请分析原任务：测试\n中断原因：未知') == '请分析原任务：测试\n中断原因：未知'
    assert error_category('API 402 Insufficient account balance') == 'billing'
    assert error_category('No endpoints found that support image input') == 'model_capability'


def project(tmp_path, main=''):
    path = tmp_path / 'PLC.plcproj'
    path.write_text('<Project><ItemGroup><Compile Include="MAIN.TcPOU"/>'
                    '<Compile Include="PRG_Main.TcPOU"/><Compile Include="PlcTask.TcTTO"/></ItemGroup></Project>')
    for name, impl in [('MAIN', main), ('PRG_Main', 'nCount := nCount + 1;')]:
        (tmp_path / (name + '.TcPOU')).write_text(f'<TcPlcObject><POU Name="{name}"><Declaration>PROGRAM {name}</Declaration>'
            f'<Implementation><ST>{impl}</ST></Implementation></POU></TcPlcObject>')
    (tmp_path / 'PlcTask.TcTTO').write_text('<TcPlcObject><Task><PouCall><Name>MAIN</Name></PouCall></Task></TcPlcObject>')
    return path


def test_empty_main_and_unreferenced_business_program(tmp_path):
    data = plc_entry_evidence(project(tmp_path))
    assert data['empty_entry_programs'] == ['MAIN']
    assert data['unreferenced_programs'] == ['PRG_Main']
    assert not data['live_verified']


def test_real_call_not_comment_or_string(tmp_path):
    path = project(tmp_path, "// PRG_Main();\nsText := 'PRG_Main()';")
    assert plc_entry_evidence(path)['unreferenced_programs'] == ['PRG_Main']
    project(tmp_path, 'PRG_Main();')
    assert plc_entry_evidence(path)['unreferenced_programs'] == []


def test_completion_invalidates_old_build_after_write(tmp_path):
    path = project(tmp_path)
    evidence = CompletionEvidence()
    with patch('tc_agent.plc_source.discover_projects', return_value=[path]):
        assert evidence.final_note('') == ''  # Read-only conversation: no mutation gate.
        evidence.record('plc_write', {'name': 'MAIN'}, {'written': True}, False)
        evidence.record('plc_build', {}, {'failedProjects': 0}, False)
        assert any('实现为空' in issue for issue in evidence.issues(''))
        evidence.record('plc_write', {'name': 'PRG_Main'}, {'written': True}, False)
        assert any('编译' in issue for issue in evidence.issues(''))


def test_agent_cannot_apply_guessed_ads_address():
    with patch('tc_agent.agent_core.ps_com') as com:
        result = run_tool('tc_hmi_ads_symbol_set', {'runtime': 'PLC1', 'name': 'GVL_HMI.eLineState',
                         'index_group': 16448, 'index_offset': 0, 'type_name': 'INT', 'apply': True})
        assert result['status'] == 'blocked' and result['written'] is False
        com.assert_not_called()


def test_preview_and_remove_remain_available():
    with patch('tc_agent.agent_core.ps_com', return_value={'status': 'preview'}) as com:
        run_tool('tc_hmi_ads_symbol_set', {'runtime': 'PLC1', 'name': 'x', 'apply': False})
        com.assert_called_once()


def test_vsix_empty_diagnostics_is_available():
    source = (Path(__file__).parents[1] / 'tc_agent_vsix/XaeBuildPipeService.cs').read_text()
    assert '((items.Count > 0) ? "true" : "false")' not in source
    assert 'diagnosticsAvailable' in source


def test_plc_write_readback_only_normalizes_editor_line_endings():
    from tc_agent.agent_core import _live_write_readback
    with patch('tc_agent.agent_core.ps_com', return_value={'implementation': 'x := 1;\r\ny := 2;'}):
        assert _live_write_readback({'name': 'MAIN'}, expected='x := 1;\ny := 2;\n')['verified']
        assert not _live_write_readback({'name': 'MAIN'}, expected='x := 9;\ny := 2;\n')['verified']
        assert not _live_write_readback({'name': 'MAIN'}, expected='x := 1;\ny := 2;  \n')['verified']


def test_legacy_ui_thread_empty_read_is_not_unavailable():
    from tc_template.xae_build_pipe import normalize_diagnostics
    assert normalize_diagnostics({'errorSource': 'dte-error-items-ui-thread', 'errorsRead': False})['errorsRead']
    assert not normalize_diagnostics({'errorSource': 'unavailable-no-focus', 'errorsRead': False})['errorsRead']


def test_read_action_and_snapshot_do_not_trigger_source_completion():
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_control_events', {'action': 'read'}, {'status': 'read'}, False)
    evidence.record('plc_snapshot', {}, {'status': 'created'}, False)
    assert not evidence.changed


def test_hmi_encoding_verifies_disk_before_success():
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    body = source.split('function Set-TcHmiDocumentText {')[1].split('\nfunction ')[0]
    assert 'UTF-8 disk readback mismatch' in body
    assert 'New-Object Text.UTF8Encoding($false, $true)' in body
    assert body.index('$doc.Close(2)') < body.index('[IO.File]::WriteAllText($full, $Text')


def test_com_busy_typeinfo_is_not_swallowed_into_unknown_solution():
    from tc_template._com import _dynamic_dispatch
    from unittest.mock import Mock
    import pytest
    class Busy(Exception):
        hresult = -2147418111
    obj = Mock()
    obj.GetTypeInfo.side_effect = Busy('RPC_E_CALL_REJECTED')
    with patch('win32com.client.dynamic.Dispatch') as dispatch:
        with pytest.raises(Busy):
            _dynamic_dispatch(obj)
        dispatch.assert_not_called()


def test_diagnostics_request_is_never_a_build():
    from tc_template.xae_build_pipe import request_diagnostics
    with patch('tc_template.xae_build_pipe.request_build', return_value={'ok': False}) as request:
        request_diagnostics(42)
        request.assert_called_once_with(42, 1500, command='diagnostics')
