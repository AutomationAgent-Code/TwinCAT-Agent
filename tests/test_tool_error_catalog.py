import ast
from pathlib import Path

import pytest

from tc_agent.tool_error_catalog import CONDITIONS, ERROR_CODES, ERROR_TYPES, FAILURE_STATUSES, FAMILIES
from tc_agent.tool_error_policy import classify_error
from tc_agent.execution_policy import tool_succeeded


@pytest.mark.parametrize('code,family', list(ERROR_TYPES.items()))
def test_error_types(code, family):
    assert classify_error({'error_type': code}, False)['family'] == family


@pytest.mark.parametrize('code,family', list(ERROR_CODES.items()))
def test_error_codes(code, family):
    assert classify_error({'error_code': code, 'status': 'blocked'}, False)['family'] == family


@pytest.mark.parametrize('condition,family', list(CONDITIONS.items()))
def test_conditions(condition, family):
    r = {'error_type': 'tool_precondition', 'condition': condition, 'not_executed': True}
    assert classify_error(r, False)['family'] == family


@pytest.mark.parametrize('status,family', list(FAILURE_STATUSES.items()))
def test_failure_statuses(status, family):
    assert not tool_succeeded({'status': status})
    assert classify_error({'status': status}, False)['family'] == family


def test_static_error_inventory_has_no_unregistered_codes():
    """New literal codes/conditions must be deliberately classified in review."""
    root = Path(__file__).resolve().parents[1]
    missing = []
    for package in ('tc_agent', 'tc_template'):
        for path in (root / package).rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                pairs = []
                if isinstance(node, ast.Dict):
                    pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                             if isinstance(k, ast.Constant)]
                elif isinstance(node, ast.Call):
                    pairs = [(k.arg, k.value) for k in node.keywords]
                    if isinstance(node.func, ast.Name) and node.func.id == '_condition' and node.args:
                        pairs.append(('condition', node.args[0]))
                for key, value in pairs:
                    catalog = {'error_type': ERROR_TYPES, 'condition': CONDITIONS, 'error_code': ERROR_CODES}.get(key)
                    if catalog is not None and isinstance(value, ast.Constant) and isinstance(value.value, str):
                        if value.value not in catalog:
                            missing.append((str(path.relative_to(root)), node.lineno, key, value.value))
    assert not missing


def test_unknown_external_code_stays_visible_and_never_grants_retry():
    r = {'error_type': 'VendorSpecific42', 'error': 'failure'}
    p = classify_error(r, False)
    assert p['classified'] is False and p['code'] == 'VendorSpecific42'
    assert 'retry_safe' not in p


def test_all_families_have_actions():
    assert all(level and impact and action for level, impact, action in FAMILIES.values())


@pytest.mark.parametrize('status,family', [('invalid', 'source'), ('incomplete', 'capability')])
def test_review_distinguishes_bad_code_from_missing_capability(status, family):
    r = {'review': {'approved': False, 'semantic_evidence': {'status': status}}}
    assert classify_error(r, False)['family'] == family
