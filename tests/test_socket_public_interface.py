import pytest
from tc_template.library_signatures import parse_signatures
from tc_template.plc_write_context import review_dependencies


PATH='TIPC^PLC^Project^POUs^MAIN'
def library(name='Tc2_TcpIp'):
    return parse_signatures('<Library><LibraryName>'+name+'</LibraryName><Version>3.4.4.0</Version><TypeSignatures>'+
        ''.join('<TypeSignature type="Type"><Name>'+n+'</Name></TypeSignature>' for n in ['T_HSOCKET','ST_SockAddr'])+
        '</TypeSignatures></Library>')


def check(code,libname='Tc2_TcpIp',shadow=False):
    def call(verb,**args):
        if verb=='find-pou':
            if shadow and args['query'].upper()=='ST_SOCKADDR':
                return {'total':1,'matches':[{'name':'ST_SockAddr','path':'TIPC^PLC^Project^DUTs^ST_SockAddr'}]}
            return {'total':0,'matches':[]}
        if verb=='read-pou': return {'declaration':'TYPE ST_SockAddr:STRUCT nPort:BOOL;END_STRUCT END_TYPE'}
        assert verb=='library-signatures'
        return {'status':'read','libraries':[library(libname)]}
    return review_dependencies({'name':'MAIN','declaration':'PROGRAM MAIN\nVAR h:T_HSOCKET; other:T_HSOCKET; b:BOOL; n:UDINT; s:STRING(15); END_VAR',
        'implementation':code},PATH,call,semantic=True)


@pytest.mark.parametrize('code',['b:=h=0;','h:=0;','b:=0<>h;'])
def test_struct_scalar_error_not_capability_gap(code):
    result=check(code)
    assert result['semantic_evidence']['status']=='invalid',result
    assert any(f['rule']=='semantic-type' and f['area']=='implementation' for f in result['findings'])
    assert not result['semantic_evidence']['unsupported_reasons']


@pytest.mark.parametrize('code',['b:=h.handle=0;','h:=other;','n:=h.localAddr.nPort;','s:=h.remoteAddr.sAddr;'])
def test_public_member_subset(code):
    result=check(code)
    assert not result['findings'],result
    assert not result['compiler_verified']


def test_wrong_library_does_not_gain_tcpip_evidence():
    result=check('b:=h.handle=0;',libname='Other')
    assert result['semantic_evidence']['status']=='incomplete'


def test_public_type_does_not_invent_binit():
    result=check('b:=h.bInit;')
    assert result['semantic_evidence']['status']=='invalid'
    assert any(f['rule']=='semantic-member' for f in result['findings'])


def test_shadowed_nested_type_not_used_as_library_type():
    result=check('n:=h.localAddr.nPort;',shadow=True)
    assert result['semantic_evidence']['status']=='incomplete'


def test_no_fabricated_layout_or_declaration():
    obj=library()['objects'][0]
    assert obj['opaque_type'] and not obj['signature_complete']
    assert 'declaration' not in obj
    assert obj['interface_knowledge']['version_scope'].endswith('not exact-version ABI proof')


def test_known_type_error_does_not_exhaust_semantic_capability():
    from tc_agent.execution_policy import FailurePolicy
    policy=FailurePolicy()
    args={'candidates':[{'path':PATH}]}
    for _ in range(2):
        result=check('b:=h=0;')
        policy.record('plc_preflight',args,result,False,True)
        assert not result.get('capability_exhausted')
    assert not policy.semantic_failures
