from tc_agent.agent_core import REGISTRY, tools_schema, cap_tool_result
from tc_agent.tool_surface import COMPATIBILITY_TOOLS, audit


def test_surface_covers_registry_and_keeps_compatibility_dispatch():
    names = {t['name'] for t in REGISTRY}
    assert len(audit(REGISTRY)) == len(names)
    exposed = {t['name'] for t in tools_schema()}
    assert names - exposed == set(COMPATIBILITY_TOOLS)
    for old, (replacement, _) in COMPATIBILITY_TOOLS.items():
        assert replacement in exposed and old in names
        assert old in {t['name'] for t in tools_schema(allowed_names={old})}


def test_large_generic_result_keeps_error_evidence():
    result = {'status': 'blocked', 'review': {
        'semantic_evidence': {'status': 'incomplete', 'unsupported_reasons': ['missing type']},
        'findings': [{'severity': 'error', 'rule': 'semantic-unresolved', 'line': 9,
                      'message': 'missing type'}] * 90}, 'raw': 'x' * 60000,
        'error_policy': {'level': 'L3', 'impact': 'dependency', 'stop_turn': False}}
    capped = cap_tool_result(result, 1800)
    d = capped['decision_evidence']
    assert d['findings_count'] == 90 and d['unsupported_reason_count'] == 1
    assert d['findings'][0]['line'] == 9
    assert d['error_policy']['stop_turn'] is False
    assert cap_tool_result(capped, 1200)['decision_evidence'] == d
