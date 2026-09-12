from unittest.mock import Mock
import pytest
from tc_template.st_preflight import review_candidate
from tc_template.plc_write_context import review_dependencies
from tc_template.plc_preflight import review_candidates

PATH = 'TIPC^PLC^PLC Project^POUs^Deep^FB_PID'


def candidate(source='CalcPidiOutput();'):
    return {'name': 'FB_PID', 'path': PATH, 'declaration': 'FUNCTION_BLOCK FB_PID\nVAR b : BOOL; END_VAR',
            'implementation': source}


@pytest.mark.parametrize('source', ['CalcPidiOutput();', 'calcpidioutput();', 'THIS^.CalcPidiOutput();'])
def test_internal_private_method(source):
    resolve = Mock(return_value=None)
    member = Mock(return_value={'declaration': 'METHOD PRIVATE CalcPidiOutput'})
    result = review_candidate(candidate(source), resolve, member)
    assert result['approved'], result
    resolve.assert_not_called()
    assert member.call_args.args[0] == 'FB_PID'


@pytest.mark.parametrize('source', ['b := Ready(n := 1);', 'b := THIS^.Ready(n := 1);'])
def test_return_and_parameter_checks(source):
    member = Mock(return_value={'declaration': 'METHOD PRIVATE Ready : BOOL\nVAR_INPUT n : INT; END_VAR'})
    assert review_candidate(candidate(source), resolve_member=member)['approved']
    assert not review_candidate(candidate(source.replace('n := 1', 'n := TRUE')), resolve_member=member)['approved']
    assert not review_candidate(candidate(source.replace('n := 1', 'wrong := 1')), resolve_member=member)['approved']
    member.return_value = {'declaration': 'METHOD PRIVATE Ready : INT\nVAR_INPUT n : INT; END_VAR'}
    assert not review_candidate(candidate(source), resolve_member=member)['approved']


def test_sibling_method_has_enclosing_scope():
    c = candidate('b := Ready();')
    c.update(declaration='METHOD PUBLIC Execute', enclosing_declaration=c['declaration'])
    assert review_candidate(c, resolve_member=lambda *_: {'declaration': 'METHOD PRIVATE Ready : BOOL'})['approved']


def test_local_instance_shadows_method():
    c = candidate('CalcPidiOutput();')
    c['declaration'] += '\nVAR CalcPidiOutput : FB_Other; END_VAR'
    member = Mock()
    result = review_candidate(c, lambda _: {'declaration': 'FUNCTION_BLOCK FB_Other'}, member)
    assert result['approved']
    member.assert_not_called()


def test_missing_method_can_resolve_global_function():
    assert review_candidate(candidate(), lambda _: {'declaration': 'FUNCTION CalcPidiOutput : BOOL'}, lambda *_: None)['approved']
    assert not review_candidate(candidate(), lambda _: None, lambda *_: None)['approved']


def test_external_private_method_still_blocked():
    c = candidate('other.Ready();')
    c['declaration'] += '\nVAR other : FB_Other; END_VAR'
    assert not review_candidate(c, lambda _: {'declaration': 'FUNCTION_BLOCK FB_Other'},
                                lambda *_: {'declaration': 'METHOD PRIVATE Ready'})['approved']


def test_unknown_pointer_and_this_outside_fb_still_blocked():
    assert not review_candidate({'declaration': 'PROGRAM MAIN', 'implementation': 'THIS^.Ready();'},
                                resolve_member=lambda *_: {'declaration': 'METHOD Ready'})['approved']
    c = candidate('p^.Ready();')
    c['declaration'] += '\nVAR p : POINTER TO FB_PID; END_VAR'
    assert not review_candidate(c, resolve_member=lambda *_: {'declaration': 'METHOD Ready'})['approved']


@pytest.mark.parametrize('decl', ['METHOD PRIVATE Wrong', 'FUNCTION CalcPidiOutput : BOOL', 'METHOD ABSTRACT CalcPidiOutput'])
def test_wrong_or_abstract_signature_not_accepted(decl):
    assert not review_candidate(candidate(), resolve_member=lambda *_: {'declaration': decl})['approved']


def test_live_dependency_uses_exact_parent_and_caches_signature():
    call = Mock(return_value={'declaration': 'METHOD PRIVATE CalcPidiOutput'})
    result = review_dependencies(candidate('CalcPidiOutput(); THIS^.CalcPidiOutput();'), PATH, call, semantic=True)
    assert result['semantic_review']['approved'], result
    call.assert_called_once_with('read-pou', name='FB_PID', path=PATH, method='CalcPidiOutput',
                                 area='declaration', include_member_code=False, start_line=1, max_lines=0)


def test_batch_does_not_return_fb_declaration_as_method_signature():
    call = Mock(return_value={'declaration': 'METHOD PRIVATE CalcPidiOutput'})
    result = review_candidates([candidate()], call)
    assert result['approved'], result
    assert call.call_args.kwargs['method'] == 'CalcPidiOutput'


def test_truncated_method_remains_blocked():
    result = review_dependencies(candidate(), PATH, Mock(return_value={
        'declaration': 'METHOD PRIVATE CalcPidiOutput', 'declaration_paging': {'truncated': True}}), semantic=True)
    assert not result['semantic_review']['approved']
