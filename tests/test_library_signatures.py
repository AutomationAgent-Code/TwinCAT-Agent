import pytest
from tc_template.library_signatures import parse_signatures
from tc_template.plc_write_context import review_dependencies


def xml(typ='BOOL'):
    return ('<Library><LibraryName>Demo</LibraryName><Version>1.0</Version><TypeSignatures>'
            '<TypeSignature type="FunctionBlock"><Name>FB_Test</Name><Inputs><Input>'
            '<Name>bExecute</Name><DataType>' + typ + '</DataType></Input></Inputs><Outputs>'
            '<Output><Name>bBusy</Name><DataType>BOOL</DataType></Output></Outputs>'
            '</TypeSignature></TypeSignatures></Library>')


def check(code, typ='BOOL'):
    calls = []
    def call(command, **args):
        calls.append(command)
        if command == 'find-pou':
            return {'matches': [], 'total': 0}
        assert command == 'library-signatures'
        return {'status': 'read', 'libraries': [parse_signatures(xml(typ))]}
    result = review_dependencies({'name': 'MAIN', 'declaration': 'PROGRAM MAIN\nVAR\nf : FB_Test;\nb : BOOL;\nEND_VAR',
                                  'implementation': code}, 'TIPC^P^P Project^POUs^MAIN', call, semantic=True)
    return result, calls


def test_valid_call_uses_live_signature_once():
    result, calls = check('f(bExecute := TRUE, bBusy => b);')
    assert not result['findings']
    assert calls.count('library-signatures') == 1
    assert result['dependencies'][0]['version'] == '1.0'
    assert result['compiler_verified'] is False


@pytest.mark.parametrize('code', ['f(wrong := TRUE);', 'f(bBusy := TRUE);', "f(bExecute := 'bad');"])
def test_invalid_calls_block(code):
    result, _ = check(code)
    assert any(f['rule'] in {'semantic-call', 'semantic-type'} for f in result['findings'])


def test_unused_parameter_type_does_not_require_recursive_resolution():
    result, calls = check('f();', 'T_Unknown')
    assert not result['findings']
    assert calls.count('library-signatures') == 1


def test_unknown_schema_not_converted_to_declaration():
    obj = parse_signatures(xml('STRING(UNKNOWN_LENGTH)'))['objects'][0]
    assert not obj['signature_complete']
    assert 'declaration' not in obj


def test_identity_and_budget_required():
    with pytest.raises(ValueError):
        parse_signatures('<Library/>')
    with pytest.raises(ValueError):
        parse_signatures('x' * 4_000_001)


@pytest.mark.parametrize('state', ['incomplete', 'ambiguous'])
def test_incomplete_or_ambiguous_library_set_blocks(state):
    def call(command, **args):
        if command == 'find-pou':
            return {'matches': [], 'total': 0}
        return {'status': 'incomplete' if state == 'incomplete' else 'read',
                'libraries': [parse_signatures(xml()), parse_signatures(xml())]}
    result = review_dependencies({'name': 'MAIN', 'declaration': 'PROGRAM MAIN\nVAR\nf : FB_Test;\nEND_VAR',
                                  'implementation': 'f();'}, 'TIPC^P^P Project^POUs^MAIN', call, semantic=True)
    assert result['semantic_evidence']['status'] == 'incomplete'
    assert not result['dependencies']


def test_no_library_call_for_ambiguous_project_lookup():
    calls = []
    def call(command, **args):
        calls.append(command)
        return {'matches': [{'name': 'FB_Test', 'path': 'TIPC^P^P Project^POUs^A'},
                            {'name': 'FB_Test', 'path': 'TIPC^P^P Project^POUs^B'}]}
    review_dependencies({'declaration': 'PROGRAM MAIN\nVAR\nf : FB_Test;\nEND_VAR', 'implementation': 'f();'},
                        'TIPC^P^P Project^POUs^MAIN', call, semantic=True)
    assert calls == ['find-pou']


@pytest.mark.parametrize('code,passed', [('f(bExecute := h);', True),
                                       ('h := h2;', True), ('h.member := 1;', False),
                                       ('f(bExecute := TRUE);', False)])
def test_opaque_handle_passthrough_not_layout(code, passed):
    library = parse_signatures(xml('T_Handle').replace('</TypeSignatures>',
        '<TypeSignature type="Type"><Name>T_Handle</Name></TypeSignature></TypeSignatures>'))
    def call(command, **args):
        if command == 'find-pou':
            return {'matches': [], 'total': 0}
        return {'status': 'read', 'libraries': [library]}
    result = review_dependencies({'name': 'MAIN', 'declaration':
        'PROGRAM MAIN\nVAR\nf:FB_Test;\nh:T_Handle;\nh2:T_Handle;\nEND_VAR',
        'implementation': code}, 'TIPC^P^P Project^POUs^MAIN', call, semantic=True)
    assert (not result['findings']) == passed


def test_prewrite_failure_ledger_is_not_uncertain():
    from unittest.mock import Mock
    from tc_agent.runtime import execution_failure_report
    store = Mock()
    store.agent_run_snapshot.return_value = {'tool_executions': [{
        'tool_name': 'plc_patch', 'status': 'failed', 'readonly': False,
        'result': {'written': False, 'failure_stage': 'pre_write_review'}}]}
    report = execution_failure_report(store, 'run', 'blocked')
    assert '写前检查未通过，未写入' in report
    assert '是否产生部分影响' not in report


def test_alias_is_bound_to_official_library_not_same_named_custom_type():
    body = '<TypeSignature type="Type"><Name>T_AmsNetId</Name></TypeSignature>'
    template = '<Library><LibraryName>{}</LibraryName><Version>3.10.2.0</Version><TypeSignatures>' + body + '</TypeSignatures></Library>'
    assert parse_signatures(template.format('Tc2_System'))['objects'][0]['public_alias'] == 'STRING(23)'
    assert 'public_alias' not in parse_signatures(template.format('Custom'))['objects'][0]


def test_preflight_advises_double_call_like_write_gate():
    from tc_template.plc_preflight import review_candidates
    objects = [dict(name='FB_Client', path='TIPC^P^P Project^POUs^FB_Client',
                    declaration='FUNCTION_BLOCK FB_Client\nVAR\nfbDemo:FB_Demo;\nEND_VAR',
                    implementation='fbDemo(); fbDemo();'),
               dict(name='FB_Demo', path='TIPC^P^P Project^POUs^FB_Demo',
                    declaration='FUNCTION_BLOCK FB_Demo', implementation='')]
    r = review_candidates(objects, lambda *a, **k: {})
    assert r['approved']
    assert any(f['rule'] == 'fb-call-count' for f in r['candidates'][0]['review']['advisories'])


def test_byte_array_address_to_byte_buffer_only():
    from tc_template.st_preflight import review_candidate
    for element, expected in [('BYTE', True), ('INT', False)]:
        r = review_candidate({'declaration': 'PROGRAM MAIN\nVAR\np:POINTER TO BYTE;\na:ARRAY[0..9] OF '+element+';\nEND_VAR',
                              'implementation': 'p := ADR(a);'})
        assert r['approved'] is expected
        assert r['pointer_safety_verified'] is False


def test_documented_alias_compatibility():
    from tc_template.st_preflight import review_candidate
    def resolve(name):
        return {'opaque_type': True, 'public_alias': 'STRING(23)'}
    r = review_candidate({'declaration': 'PROGRAM MAIN\nVAR\ns:T_AmsNetId;\nEND_VAR',
                          'implementation': "s := '';"}, resolve)
    assert r['approved']
