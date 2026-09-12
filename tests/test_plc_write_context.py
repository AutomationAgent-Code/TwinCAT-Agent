from unittest.mock import Mock, patch

from tc_template.plc_write_context import review_dependencies, non_ascii_literal
from tc_agent import agent_core as ac

PATH = 'TIPC^PLC1^PLC1 Project^POUs^MAIN'


def check(code, declaration='', objects=None):
    objects = objects or {}
    def call(verb, **args):
        name = args['query'] if verb == 'find-pou' else args['name']
        obj = objects.get(name.upper())
        if verb == 'find-pou':
            return {'matches': [{'name': name, 'path': PATH.rsplit('^', 1)[0] + '^' + name}] if obj else []}
        return {'declaration': obj}
    return review_dependencies({'name': 'MAIN', 'declaration': declaration,
                                'implementation': code}, PATH, call)


def rules(result):
    return {f['rule'] for f in result['findings'] if f['severity'] == 'error'}


def test_missing_gvl_member_and_success():
    objects = {'GVL_HMI': 'VAR_GLOBAL\n bStart : BOOL;\nEND_VAR'}
    assert 'dependency-member-missing' in rules(check('GVL_Hmi.nBatchProgress := 1;', objects=objects))
    assert not rules(check('GVL_Hmi.bStart := TRUE;', objects=objects))


def test_integer_to_enum_and_explicit_mapping():
    decl = 'VAR\n eState : E_LineState;\n nState : DINT;\nEND_VAR'
    objects = {'E_LINESTATE': 'TYPE E_LineState : (Idle, Running); END_TYPE'}
    assert 'dependency-enum-assignment' in rules(check('eState := nState;', decl, objects))
    assert not rules(check('eState := E_LineState.Idle;', decl, objects))


def test_unknown_dependency_blocks_without_claiming_missing_member():
    result = check('GVL_Hmi.bStart := TRUE;')
    assert rules(result) == {'dependency-context-unavailable'}
    assert result['compiler_verified'] is False


def test_does_not_read_other_plc_or_ambiguous_names():
    for paths in [['TIPC^PLC2^PLC2 Project^GVLs^GVL_Hmi'],
                  [PATH + '^A', PATH + '^B']]:
        call = Mock(return_value={'matches': [{'name': 'GVL_Hmi', 'path': p} for p in paths]})
        result = review_dependencies({'implementation': 'GVL_Hmi.x := 1;'}, PATH, call)
        assert 'dependency-context-unavailable' in rules(result)
        assert call.call_count == 1


def test_comments_strings_shadowing_and_unsupported_expressions():
    assert not rules(check("// GVL_Hmi.missing := 1;\ns := 'GVL_Hmi.x';"))
    assert not rules(check('GVL_Hmi.x := 1;', 'VAR\n GVL_Hmi : FB_Demo;\nEND_VAR'))
    assert not rules(check('eState := SomeFunction(nState);'))
    assert not non_ascii_literal('// 中文\n(* 嵌套 (* 中文 *) *) s := \'OK\';')
    assert non_ascii_literal("s := '上料';")
    assert not non_ascii_literal('s := "上料";')


def test_obvious_implementation_syntax_and_encoding_advisory():
    result = check("VAR\nn : INT;\nEND_VAR\nn := INT#(x);\ns := '上料';")
    assert rules(result) == {'implementation-declaration', 'invalid-typed-literal'}
    assert any(f['rule'] == 'encoding-review' and f['severity'] == 'warning' for f in result['findings'])


def test_truncated_baseline_prevents_mutation():
    with patch.object(ac, 'ps_com', return_value={'declaration_paging': {'truncated': True}}) as call:
        result = ac._guarded_plc_write({'name': 'MAIN', 'area': 'implementation', 'code': 'x := 1;'})
    assert result['written'] is False
    assert call.call_count == 1


def test_live_missing_dependency_prevents_write_even_with_soft_gate_off():
    current = {'name': 'MAIN', 'path': PATH, 'declaration': 'PROGRAM MAIN', 'implementation': ''}
    with patch.object(ac, 'ps_com', side_effect=[current, {'matches': []}, {'status': 'incomplete'}]) as call, \
         patch('tc_agent.config.load_config', return_value={'quality_gate_enabled': False}):
        result = ac._guarded_plc_write({'name': 'MAIN', 'area': 'implementation', 'code': 'GVL_Hmi.x := 1;'})
    assert result['written'] is False
    assert [c.args[0] for c in call.call_args_list] == ['read-pou', 'find-pou', 'library-signatures', 'library-evidence']


def test_complete_append_readback_is_not_compiler_verification():
    with patch.object(ac, '_review_pou_write', return_value={'approved': True, '_expected_area': 'old\nnew'}), \
         patch.object(ac, 'ps_com', side_effect=[{}, {'declaration': 'old\nnew'}]):
        result = ac._guarded_plc_patch({'name': 'GVL', 'area': 'declaration', 'old_text': 'old', 'new_text': 'old\nnew'})
    assert result['verified'] and result['compiler_verified'] is False


def test_mutation_exception_and_readback_exception_keep_distinct_stages():
    for responses, stage, written in [
        ([RuntimeError('COM timeout')], 'com_write', None),
        ([{}, RuntimeError('read timeout')], 'post_write_readback', True),
    ]:
        with patch.object(ac, '_review_pou_write', return_value={'approved': True}), \
             patch.object(ac, 'ps_com', side_effect=responses):
            result = ac._guarded_plc_write({'name': 'MAIN', 'area': 'implementation', 'code': ''})
        assert result['failure_stage'] == stage and result['written'] is written
        assert result['retry_safe'] is False and result['verified'] is False


def test_creation_with_dependency_requires_scope_before_any_mutation():
    with patch.object(ac, 'ps_com') as call, \
         patch('tc_agent.config.load_config', return_value={'quality_gate_enabled': False}):
        result = ac._guarded_plc_create({'name': 'PRG_Demo', 'type': 'program',
            'declaration': 'PROGRAM PRG_Demo', 'implementation': 'GVL_Hmi.x := 1;'})
    assert result['written'] is False
    call.assert_not_called()


def test_dependency_read_failure_and_budget_are_not_passed():
    candidate = {'implementation': 'GVL_Hmi.x := 1;'}
    result = review_dependencies(candidate, PATH, Mock(side_effect=RuntimeError('disconnected')))
    assert 'dependency-context-unavailable' in rules(result)
    call = Mock()
    assert 'dependency-context-unavailable' in rules(review_dependencies(candidate, PATH, call, limit=0))
    call.assert_not_called()


def test_truncated_dependency_is_not_absent_member():
    call = Mock(side_effect=[{'matches': [{'name': 'GVL_Hmi', 'path': PATH + '^GVL_Hmi'}]},
                            {'declaration': '', 'declaration_paging': {'truncated': True}}])
    result = review_dependencies({'implementation': 'GVL_Hmi.x := 1;'}, PATH, call)
    assert rules(result) == {'dependency-context-unavailable'}
