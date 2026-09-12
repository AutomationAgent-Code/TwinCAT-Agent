import pytest

from tc_template.plc_constraints import constraints, prompt_contract
from tc_template.lint import review_write_candidate
from tc_agent.completion_evidence import CompletionEvidence


def test_rule_query_distinguishes_guidance_from_executable_coverage():
    assert constraints('SA0040')['rules'][0]['checker'] == 'TCSA0040'
    assert constraints('sa0006')['rules'][0]['coverage'] == 'author_review'
    assert constraints()['license_required'] is False
    assert constraints()['te1200_executed'] is False
    with pytest.raises(ValueError):
        constraints('SA9999')


def test_catalog_does_not_share_mutable_state():
    first = constraints()
    first['rules'].clear()
    assert len(constraints()['rules']) == 29


def test_official_semantics_are_preserved_in_guidance():
    assert 'qualified_only' in constraints('SA0027')['rules'][0]['requirement']
    assert '自己的 VAR_OUTPUT' in constraints('SA0038')['rules'][0]['requirement']
    assert '默认复杂度上限20' in constraints('SA0178')['rules'][0]['requirement']
    assert constraints('SA0038')['rules'][0]['checker'] == 'TCSA0038'


def test_runtime_prompt_and_tool_have_no_com_or_license_requirement():
    from tc_agent.backend import _system_prompt
    from tc_agent.agent_core import REGISTRY
    assert prompt_contract() in _system_prompt('demo.sln', 'project')
    tool = next(t for t in REGISTRY if t['name'] == 'plc_static_constraints')
    assert tool['readonly']
    from tc_agent.backend import _auto_tool_categories
    selected = _auto_tool_categories('编写 PLC 程序')
    assert tool['category'] in selected
    analysis = next(t for t in REGISTRY if t['name'] == 'plc_static_analysis')
    assert analysis['category'] in selected
    assert tool['run']({'rule_id': 'SA0075'})['rules'][0]['checker'] == 'TCSA0075'


def test_new_candidate_gate_blocks_real_defect_but_not_fractional_divisor():
    candidate = {'name': 'MAIN', 'folder': 'POUs', 'declaration': 'PROGRAM MAIN',
                 'implementation': 'x := 1 / 0.5;', 'methods': []}
    assert review_write_candidate(candidate, changed_area='implementation')['approved']
    candidate['implementation'] = 'x := 1 / 0;'
    review = review_write_candidate(candidate, changed_area='implementation')
    assert not review['approved']
    assert any(f['rule'] == 'TCSA0040' for f in review['blocking_findings'])


def test_analysis_errors_cannot_count_as_completion_evidence():
    evidence = CompletionEvidence()
    evidence.record('plc_static_analysis', {}, {'summary': {'errors': 0}}, True)
    assert 'plc_static_analysis' in evidence.checked
    evidence.record('plc_static_analysis', {}, {'summary': {'errors': 1}}, True)
    assert 'plc_static_analysis' not in evidence.checked
    assert evidence.issues('')
    evidence.record('plc_static_analysis', {}, {'summary': {'errors': 0}}, True)
    assert not evidence.issues('')
