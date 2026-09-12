from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import pytest

from tc_template import document_save as ds
from tc_agent import agent_core as ac


class Node:
    Name = 'FB_Test'
    ItemType = 604
    DeclarationText = 'FUNCTION_BLOCK FB_Test\r\nVAR\r\nEND_VAR'
    ImplementationText = 'x := 1;'
    def __iter__(self):
        return iter([])


@pytest.fixture
def scene(tmp_path):
    system = tmp_path / 'System.tsproj'
    project = tmp_path / 'PLC.plcproj'
    file = tmp_path / 'POUs' / 'FB_Test.TcPOU'
    file.parent.mkdir()
    system.write_text('<Root><Project Name="PLC" PrjFilePath="PLC.plcproj"/></Root>')
    project.write_text('<Project><Compile Include="POUs/FB_Test.TcPOU"/></Project>')
    xml = '<TcPlcObject><POU Name="FB_Test"><Declaration>FUNCTION_BLOCK FB_Test\nVAR\nEND_VAR</Declaration><Implementation><ST>x := 1;</ST></Implementation></POU></TcPlcObject>'
    file.write_text(xml)
    doc = NS(FullName=str(file), Saved=False)
    def save(target):
        assert target == str(file)
        doc.Saved = True
        file.write_text(xml)
        return 0
    doc.Save = Mock(side_effect=save)
    dte = NS(Documents=[doc], Solution=NS(FullName=str(tmp_path / 'Project.sln')), MainWindow=NS(HWnd=12))
    return dte, Node(), 'TIPC^PLC^PLC Project^POUs^FB_Test', system, doc, file


def call(scene, **kwargs):
    return ds.execute(*scene[:4], **kwargs)


def test_preview_and_single_save(scene):
    before = call(scene)
    assert before['status'] == 'read'
    scene[4].Save.assert_not_called()
    result = call(scene, expected=before['document_baseline'], save=True)
    assert result['verified'] is True
    assert result['compiled'] is False
    assert result['status'] == 'saved'
    scene[4].Save.assert_called_once()


@pytest.mark.parametrize('change', ['live', 'disk', 'solution', 'window', 'closed', 'saved', 'mapping'])
def test_stale_baseline_never_saves(scene, change):
    baseline = call(scene)['document_baseline']
    dte, node, _, system, doc, file = scene
    if change == 'live': node.ImplementationText = 'x := 2;'
    if change == 'disk': file.write_text('<changed/>')
    if change == 'solution': dte.Solution.FullName = 'other.sln'
    if change == 'window': dte.MainWindow.HWnd = 99
    if change == 'closed': dte.Documents = []
    if change == 'saved': doc.Saved = True
    if change == 'mapping': system.write_text('<Root/>')
    result = call(scene, expected=baseline, save=True)
    assert result['not_executed'] is True
    doc.Save.assert_not_called()


@pytest.mark.parametrize('failure', ['throw', 'cancel', 'dirty', 'disk', 'live'])
def test_save_uncertainty_never_retries(scene, failure):
    baseline = call(scene)['document_baseline']
    doc, file = scene[4:]
    def save(_):
        if failure == 'throw': raise RuntimeError('busy')
        if failure == 'cancel': return 1
        doc.Saved = failure != 'dirty'
        if failure == 'disk': file.write_text('<wrong/>')
        if failure == 'live': scene[1].ImplementationText = 'changed'
        return 0
    doc.Save.side_effect = save
    result = call(scene, expected=baseline, save=True)
    assert result['status'] == 'uncertain'
    assert result['retry_safe'] is False
    doc.Save.assert_called_once()


def test_already_saved_verifies_without_save(scene):
    scene[4].Saved = True
    baseline = call(scene)['document_baseline']
    assert call(scene, expected=baseline, save=True)['status'] == 'already_saved'
    scene[4].Save.assert_not_called()


def test_duplicate_open_docs_rejected(scene):
    scene[0].Documents.append(scene[4])
    assert call(scene)['not_executed']


def test_member_only_document_not_substituted(scene):
    scene[4].FullName += '@Method'
    assert call(scene)['not_executed']


@pytest.mark.parametrize('mode', ['auto', 'accept', 'ask'])
def test_per_call_approval(mode):
    assert ac.decide(mode, 'plc_save_document') == 'ask'
    assert ac.gate(mode, 'plc_save_document', {}, None)[0] is False
    assert ac.decide('plan', 'plc_save_document') == 'deny'


def test_baseline_preserved_for_model(scene):
    result = call(scene)
    assert ac.tool_result_for_context('plc_read', {'document_baseline': True}, result)['document_baseline'] == result['document_baseline']


def test_native_transport_no_fallback():
    from tc_template import _ps_bridge as bridge
    with patch.object(bridge, '_native_request', return_value={'status': 'conflict'}) as native:
        token = bridge._TOOL_TARGET_PID.set(123)
        try:
            assert bridge.ps_com('save-document', path='TIPC^PLC^PLC Project^POUs^FB_Test')['status'] == 'conflict'
            assert native.call_args.args[2]['strictPid'] is True
        finally:
            bridge._TOOL_TARGET_PID.reset(token)


def test_transport_loss_is_uncertain():
    from tc_template import _ps_bridge as bridge
    with patch.object(bridge, '_native_request', side_effect=RuntimeError('timeout')) as native:
        with bridge.tool_target(123):
            result = bridge.ps_com('save-document', path='TIPC^PLC^PLC Project^POUs^FB_Test')
        assert result['status'] == 'uncertain'
        assert result['written'] == 'unknown'
        assert not result['retry_safe']
        native.assert_called_once()


def test_save_dependency_includes_baseline_read():
    from tc_agent.tool_recovery import recovery_dependencies
    assert 'plc_read' in recovery_dependencies(['plc_save_document'])


def test_non_st_does_not_save(scene):
    scene[5].write_text('<TcPlcObject><POU Name="FB_Test"><Implementation><LD/></Implementation></POU></TcPlcObject>')
    assert call(scene)['not_executed']
    scene[4].Save.assert_not_called()


def test_saved_extra_member_is_not_verified(scene):
    scene[4].Saved = True
    file = scene[5]
    file.write_text(file.read_text().replace('</POU>', '<Method Name="Unexpected"/></POU>'))
    assert call(scene)['not_executed']


@pytest.mark.parametrize('kind,tag', [(615, 'GVL'), (606, 'DUT'), (618, 'Itf')])
def test_declaration_objects_can_be_verified(scene, kind, tag):
    scene[1].ItemType = kind
    file = scene[5]
    file.write_text(file.read_text().replace('<POU ', '<' + tag + ' ').replace('</POU>', '</' + tag + '>'))
    scene[4].Saved = True
    assert call(scene)['disk_live_match'] is True
