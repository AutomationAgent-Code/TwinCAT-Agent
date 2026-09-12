from xml.sax.saxutils import escape
from unittest.mock import patch
from tc_template.library_evidence import inspect_reference
from tc_agent import agent_core as ac


def test_exact_reference_not_latest(tmp_path):
    old = tmp_path / '3.4.2.0'; old.mkdir()
    file = old / 'lib.compiled-library'; file.write_bytes(b'fixture')
    (old / 'browsercache').write_text('<Library><Node Name="FB_SocketConnect"/></Library>')
    xml = '<TreeItem><PlcLibDef><Library><LibraryName>Tc2_TcpIp</LibraryName><Version>*</Version><EffectiveVersion>3.4.2.0</EffectiveVersion><RelativePath>'+escape(str(file))+'</RelativePath></Library></PlcLibDef></TreeItem>'
    result = inspect_reference(xml)
    assert result['effective_version'] == '3.4.2.0'
    assert result['symbols'] == ['FB_SocketConnect']
    assert not result['signature_verified']


def test_missing_signature_is_not_approval():
    context = {'findings': [{'message': 'Unresolved declaration/library symbol: FB_SOCKETCONNECT'}],
               'semantic_evidence': {'status': 'incomplete'}}
    with patch.object(ac, 'ps_com', return_value={'libraries': [{'name': 'Tc2_TcpIp', 'effective_version': '3.4.4.0',
               'signature_verified': False, 'symbols': ['FB_SocketConnect']}]}) as read:
        ac._attach_library_signature_evidence(context, 'TIPC^PLC^Project^POUs^FB_Test')
    assert context['semantic_evidence']['status'] == 'incomplete'
    assert context['semantic_evidence']['missing_library_signatures'][0]['effective_version'] == '3.4.4.0'
    assert read.call_args.kwargs['path'] == 'TIPC^PLC^Project'
