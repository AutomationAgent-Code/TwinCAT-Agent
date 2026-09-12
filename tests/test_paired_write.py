import json
from unittest.mock import patch

import pytest

from tc_template.paired_write import write_pair
from tc_template.plc_preflight import review_candidates
from tc_agent.reporting import _recent_tool_events
from tc_agent import agent_core as ac


class Item:
    DeclarationText = 'old decl'
    ImplementationText = 'old impl'


@pytest.mark.parametrize('newline', ['\r\n', '\n', '\r'])
def test_read_paging_baseline_matches_native_text(newline):
    from tc_template._native_bridge import _slice_lines
    item = Item()
    item.DeclarationText = newline.join(['old', 'decl', ''])
    item.ImplementationText = 'old impl' + newline
    baseline = [_slice_lines(getattr(item, attr), 1, 0)[0]
                for attr in ('DeclarationText', 'ImplementationText')]
    assert write_pair(item, 'new decl', 'new impl', baseline)['verified']


def test_setter_newline_normalization_does_not_trigger_rollback():
    class Normalizing(Item):
        def __setattr__(self, key, value):
            super().__setattr__(key, value.replace('\r\n', '\n').replace('\n', '\r\n') + '\r\n')
    item = Normalizing()
    result = write_pair(item, 'new\ndecl', 'new\nimpl', ['old decl', 'old impl'])
    assert result['verified'], result


@pytest.mark.parametrize('changed', ['old  decl', 'OLD decl', 'old decl ', 'user edit'])
def test_real_source_difference_still_blocks_with_evidence(changed):
    item = Item()
    item.DeclarationText = changed
    result = write_pair(item, 'new decl', 'new impl', ['old decl', 'old impl'])
    assert result['not_executed'] and result['changed_areas'] == ['declaration']
    assert result['expected_hashes']['declaration'] != result['actual_hashes']['declaration']
    assert item.DeclarationText == changed


@pytest.mark.parametrize('baseline', [None, [], ['old decl'], [None, 'old impl']])
def test_malformed_baseline_never_writes(baseline):
    result = write_pair(Item(), 'new decl', 'new impl', baseline)
    assert result['not_executed'] and not result['baseline_valid']


def test_pair_success_and_stale_baseline():
    item = Item()
    assert write_pair(item, 'new decl', 'new impl', ['stale', 'old impl'])['not_executed']
    result = write_pair(item, 'new decl', 'new impl', ['old decl', 'old impl'])
    assert result['verified'] and item.ImplementationText == 'new impl'


def test_second_setter_failure_restores_first():
    class Failing(Item):
        def __setattr__(self, key, value):
            if key == 'ImplementationText' and value == 'new impl':
                raise RuntimeError('setter failed')
            super().__setattr__(key, value)
    item = Failing()
    result = write_pair(item, 'new decl', 'new impl', ['old decl', 'old impl'])
    assert result['rollback_verified'] and item.DeclarationText == 'old decl'


def test_rollback_preserves_original_crlf_text():
    class Failing(Item):
        DeclarationText = 'old\r\ndecl\r\n'
        def __setattr__(self, key, value):
            if key == 'ImplementationText':
                raise RuntimeError('setter failed')
            super().__setattr__(key, value)
    item = Failing()
    result = write_pair(item, 'new decl', 'new impl', ['old\ndecl', 'old impl'])
    assert result['rollback_verified'], result
    assert item.DeclarationText == 'old\r\ndecl\r\n'


def test_rollback_failure_is_uncertain():
    class Failing(Item):
        def __setattr__(self, key, value):
            if key == 'ImplementationText' or value == 'old decl':
                raise RuntimeError('failure')
            super().__setattr__(key, value)
    assert write_pair(Failing(), 'new decl', 'new impl', ['old decl', 'old impl'])['status'] == 'uncertain'


def test_empty_preflight_is_rejected():
    with pytest.raises(ValueError, match='must not be empty'):
        review_candidates([{'name': 'FB', 'path': 'TIPC^P^Proj^POUs^FB',
                            'declaration': '', 'implementation': ''}], lambda *a, **kw: {})


def test_report_parses_json_before_redaction_and_preserves_late_findings():
    raw = json.dumps({'declaration': 'SECRET SOURCE', 'padding': 'x' * 6000,
                      'findings': [{'message': 'bActive unresolved', 'line': 88}]})
    r = _recent_tool_events([{'type': 'tool_result', 'result': raw}])[0]['result']
    assert isinstance(r, dict) and r['findings'][0]['line'] == 88
    assert 'SECRET SOURCE' not in json.dumps(r)


def test_review_uses_both_candidates_not_old_implementation():
    current = {'name': 'FB_Test', 'itemType': 604, 'path': 'TIPC^P^Proj^POUs^FB_Test',
               'declaration': 'old decl', 'implementation': 'bActive := TRUE;'}
    def quality(candidate, **kwargs):
        return {'approved': True, 'blocking_findings': [], 'advisories': []}
    with patch.object(ac, 'ps_com', return_value=current), \
         patch.object(ac, 'review_write_candidate', side_effect=quality) as check, \
         patch.object(ac, '_attach_plc_dependencies') as deps:
        review = ac._review_pou_write({'name': 'FB_Test', 'area': 'declaration',
                                       'code': 'new decl', 'implementation': 'new impl'})
    assert deps.call_args.args[1]['implementation'] == 'new impl'
    assert check.call_args.kwargs['changed_area'] == 'all'
    assert review['_paired_baseline'] == ['old decl', 'bActive := TRUE;']


def test_schema_exposes_paired_implementation():
    assert 'implementation' in ac._BY_NAME['plc_write']['parameters']['properties']


def test_late_diagnostic_survives_report_export():
    raw = json.dumps({'findings': [{'message': 'warning'}] * 100 + [{'message': 'last error'}]})
    result = _recent_tool_events([{'type': 'tool_result', 'result': raw}])[0]['result']
    assert len(result['findings']) == 101
    assert result['findings'][-1]['message'] == 'last error'


def test_concurrent_unknown_text_is_not_rolled_back():
    class Concurrent(Item):
        def __setattr__(self, key, value):
            super().__setattr__(key, value)
            if key == 'DeclarationText' and value == 'new decl':
                super().__setattr__('ImplementationText', 'user edit')
    item = Concurrent()
    result = write_pair(item, 'new decl', 'new impl', ['old decl', 'old impl'])
    assert result['status'] == 'uncertain'
    assert item.ImplementationText == 'user edit'
