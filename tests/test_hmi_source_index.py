import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tc_agent import hmi_source_index as idx
from tc_agent.plc_cache import CACHE


def fixture(tmp_path, name='LineHMI', count=78):
    sln = tmp_path / 'A.sln'
    project = tmp_path / name / f'{name}.hmiproj'
    project.parent.mkdir(parents=True)
    sln.write_text(f'Project("{{type}}") = "{name}", "{name}\\{name}.hmiproj", "{{id}}"\nEndProject', encoding='utf-8')
    project.write_text('<Project><ItemGroup><Content Include="Desktop.view"/><Content Include="Scripts/deep/a.js"/></ItemGroup></Project>', encoding='utf-8')
    view = project.parent / 'Desktop.view'
    view.write_text('<div>' + ''.join(f'<div id="C{i}" data-tchmi-type="Text" data-tchmi-text="%s%ADS.PLC1.GVL.x%/s%"></div>' for i in range(count)) + '</div>', encoding='utf-8')
    script = project.parent / 'Scripts/deep/a.js'
    script.parent.mkdir(parents=True)
    script.write_text('/* 中文😀 */\nlet a=1;\n', encoding='utf-8')
    return str(sln), project, view, script


def test_cold_warm_precise_read_no_project_scan_or_reparse(tmp_path):
    sln, project, view, _ = fixture(tmp_path)
    first = idx.read(sln, 'Desktop.view', control_id='C77')
    assert not first['cache_hit'] and first['controls'][0]['id'] == 'C77'
    with patch.object(idx, '_stable_text', side_effect=AssertionError('warm read reparsed source')):
        second = idx.read(sln, 'desktop.view', control_id='C77')
    assert second['cache_hit'] and second['source_hash'] == first['source_hash']
    assert not second['live_xae'] and second['dirty_unknown'] and not second['authoritative']
    with sqlite3.connect(project.parent.parent / '.TwinCATAgent/hmi_source_index.sqlite') as c:
        assert c.execute('select count(*) from hmi_files').fetchone()[0] == 1


def test_catalog_incremental_sync_and_control_paging(tmp_path):
    sln, _, _, _ = fixture(tmp_path)
    status = idx.sync(sln)
    assert status['projects'][0]['added'] == 2
    again = idx.sync(sln)
    assert again['projects'][0]['unchanged'] == 2
    first = idx.catalog(sln, kind='controls', limit=60)
    second = idx.catalog(sln, kind='controls', offset=first['next_offset'])
    assert len(first['items']) == 60 and len(second['items']) == 18
    assert second['next_offset'] is None
    assert idx.catalog(sln, kind='controls', query='C77')['items'][0]['control_id'] == 'C77'


def test_file_changes_and_revision_invalidate_cached_source(tmp_path):
    sln, _, view, _ = fixture(tmp_path, count=1)
    before = idx.read(sln, 'Desktop.view')
    view.write_text('<div id="Changed" data-tchmi-type="Text"></div>', encoding='utf-8')
    after = idx.read(sln, 'Desktop.view')
    assert after['controls'][0]['id'] == 'Changed' and not after['cache_hit']
    assert after['source_hash'] != before['source_hash']
    CACHE.invalidate()
    assert not idx.read(sln, 'Desktop.view')['cache_hit']
    assert not idx.read(sln, 'Desktop.view', refresh=True)['cache_hit']


def test_source_unicode_pages_and_config_auto(tmp_path):
    sln, _, _, script = fixture(tmp_path)
    expected = script.read_bytes().decode('utf-8')
    text, offset = '', 0
    while True:
        result = idx.read(sln, 'Scripts/deep/a.js', max_chars=1, content_offset=offset)
        assert result['area'] == 'source' and not result['controls']
        text += result['content']
        offset = result['next_content_offset']
        if offset is None:
            break
    assert text == expected


def test_no_cross_project_fallback_path_escape_or_directory_scan(tmp_path):
    sln, project, _, _ = fixture(tmp_path)
    secret = tmp_path / 'secret.json'
    secret.write_text('secret', encoding='utf-8')
    project.write_text(project.read_text().replace('</ItemGroup>', '<Content Include="../secret.json"/><Content Include="**/*.view"/></ItemGroup>'))
    status = idx.sync(sln)
    assert status['projects'][0]['issue_count'] == 2 and not status['complete']
    with pytest.raises(ValueError, match='not a supported saved reference'):
        idx.read(sln, '../secret.json')
    with pytest.raises(ValueError, match='exact saved'):
        idx.read(str(tmp_path), 'Desktop.view')


def test_nested_same_basename_is_resolved_by_exact_relative_include(tmp_path):
    sln, project, view, _ = fixture(tmp_path, count=1)
    nested = project.parent / '00 Content/Desktop.view'
    nested.parent.mkdir()
    nested.write_text('<div id="Nested" data-tchmi-type="Text"></div>', encoding='utf-8')
    project.write_text(project.read_text().replace('</ItemGroup>',
        '<Content Include="00 Content/Desktop.view"/></ItemGroup>'), encoding='utf-8')
    assert idx.read(sln, 'Desktop.view')['controls'][0]['id'] == 'C0'
    assert idx.read(sln, '00 Content/Desktop.view')['controls'][0]['id'] == 'Nested'


def test_duplicate_exact_include_is_deduplicated_for_generated_te2000_sections(tmp_path):
    sln, project, view, _ = fixture(tmp_path, count=1)
    project.write_text(project.read_text().replace('</ItemGroup>',
        '<Content Include="Desktop.view"/></ItemGroup>'), encoding='utf-8')
    status = idx.sync(sln)
    assert status['complete']
    assert status['projects'][0]['added'] == 2


def test_index_accepts_te2000_generated_special_filenames_without_weakening_writes(tmp_path):
    sln, project, _, _ = fixture(tmp_path, count=1)
    special = project.parent / 'KeyboardLayouts' / 'Numpad (plusminus).keyboard.json'
    special.parent.mkdir()
    special.write_text('{"name":"Numpad"}', encoding='utf-8')
    project.write_text(project.read_text().replace('</ItemGroup>',
        '<None Include=".gitignore"/><Content Include="KeyboardLayouts/Numpad (plusminus).keyboard.json"/></ItemGroup>'),
        encoding='utf-8')
    status = idx.sync(sln)
    assert status['complete']
    files = idx.catalog(sln, kind='files')['items']
    assert any(item['file'] == 'KeyboardLayouts/Numpad (plusminus).keyboard.json' for item in files)


def test_deleted_and_removed_references_never_return_stale_rows(tmp_path):
    sln, project, view, _ = fixture(tmp_path)
    idx.sync(sln)
    view.unlink()
    assert not idx.sync(sln)['complete']
    with pytest.raises(FileNotFoundError):
        idx.read(sln, 'Desktop.view')
    project.write_text('<Project/>', encoding='utf-8')
    assert idx.catalog(sln)['total'] == 0
    with pytest.raises(ValueError, match='not a supported saved reference'):
        idx.read(sln, 'Scripts/deep/a.js')


def test_malformed_and_duplicate_markup_do_not_reuse_old_controls(tmp_path):
    sln, _, view, _ = fixture(tmp_path, count=1)
    idx.read(sln, 'Desktop.view')
    view.write_text('<bad', encoding='utf-8')
    with pytest.raises(ValueError, match='Saved markup cannot be indexed'):
        idx.read(sln, 'Desktop.view')
    result = idx.read(sln, 'Desktop.view', area='source')
    assert result['content'] == '<bad' and result['parse_error'] and not result['controls']
    view.write_text('<div><div id="X" data-tchmi-type="Text"/><div id="X" data-tchmi-type="Text"/></div>')
    with pytest.raises(ValueError, match='duplicate'):
        idx.read(sln, 'Desktop.view')


def test_multiple_hmi_projects_require_explicit_selection(tmp_path):
    sln, _, _, _ = fixture(tmp_path)
    other = tmp_path / 'Other/Other.hmiproj'
    other.parent.mkdir()
    other.write_text('<Project/>')
    with Path(sln).open('a', encoding='utf-8') as f:
        f.write('\nProject("{type}") = "Other", "Other\\Other.hmiproj", "{id2}"\nEndProject')
    with pytest.raises(ValueError, match='Select one exact'):
        idx.read(sln, 'Desktop.view')
    assert idx.read(sln, 'Desktop.view', project='LineHMI')['project'] == 'LineHMI'
    with pytest.raises(ValueError, match='not a supported saved reference'):
        idx.read(sln, 'Desktop.view', project='Other')


def test_independent_solutions_and_indexes(tmp_path):
    left, _, _, _ = fixture(tmp_path / 'left', count=1)
    right, _, view, _ = fixture(tmp_path / 'right', count=2)
    assert idx.read(left, 'Desktop.view')['total_control_count'] == 1
    assert idx.read(right, 'Desktop.view')['total_control_count'] == 2
    assert idx.read(left, 'Desktop.view')['total_control_count'] == 1


def test_event_and_binding_reads_are_exact(tmp_path):
    sln, _, view, _ = fixture(tmp_path, count=1)
    view.write_text('<div id="B" data-tchmi-type="Button" data-tchmi-text="%s%ADS.PLC1.X%/s%" data-tchmi-trigger="[]"></div>')
    event = idx.read(sln, 'Desktop.view', control_id='B', area='events')
    assert event['events'] == [{'control': 'B', 'trigger': '[]'}]
    assert 'attributes' not in event['controls'][0]
    binding = idx.read(sln, 'Desktop.view', control_id='B', area='bindings')
    assert binding['bindings'][0]['expression'] == '%s%ADS.PLC1.X%/s%'


def test_script_attributes_and_multiletter_bindings(tmp_path):
    sln, _, view, _ = fixture(tmp_path, count=1)
    view.write_text('<div id="B" data-tchmi-type="Button" data-tchmi-text="%ctrl%A::Text%/ctrl%">'
                    '<script type="application/json" data-tchmi-target-attribute="data-tchmi-trigger">'
                    '[{"event":"B.onPressed","actions":[]}]</script></div>')
    event = idx.read(sln, 'Desktop.view', control_id='B', area='events')
    assert json.loads(event['events'][0]['trigger'])[0]['event'] == 'B.onPressed'
    assert event['bindings'][0]['expression'] == '%ctrl%A::Text%/ctrl%'


def test_agent_facade_uses_bound_solution_without_com(tmp_path):
    from tc_agent import agent_core as ac
    from tc_agent.plc_cache import cache_scope
    sln, _, _, _ = fixture(tmp_path)
    with cache_scope(123, sln), patch('tc_template.hmi_source.ps_com', side_effect=AssertionError('COM forbidden')):
        result = ac.run_tool('tc_hmi_read_smart', {'file': 'Desktop.view', 'control_id': 'C0'})
        assert result['controls'][0]['id'] == 'C0'
        assert result['dirty_unknown'] is True and result['live_xae'] is False
        assert ac.run_tool('tc_hmi_source_catalog', {'kind': 'files', 'file': ''})['total'] == 2


def test_context_catalog_cursor_covers_all_items(tmp_path):
    from tc_agent import agent_core as ac
    sln, _, _, _ = fixture(tmp_path, count=200)
    seen, offset = [], 0
    while True:
        result = idx.catalog(sln, kind='controls', offset=offset, limit=100)
        out = ac.tool_result_for_context('tc_hmi_source_catalog', {}, result)
        assert len(json.dumps(out, ensure_ascii=False)) <= 6000
        seen.extend(r['control_id'] for r in out['items'])
        offset = out['next_offset']
        if offset is None:
            break
    assert seen == [f'C{i}' for i in range(200)]


def test_context_preserves_atomic_events_and_pages(tmp_path):
    from tc_agent import agent_core as ac
    sln, _, view, _ = fixture(tmp_path)
    view.write_text('<div>' + ''.join('<div id="C%d" data-tchmi-type="Button" '
                    'data-tchmi-trigger="%s"></div>' % (i, 'x' * 200) for i in range(78)) + '</div>')
    seen, offset = [], 0
    while True:
        result = idx.read(sln, 'Desktop.view', area='events', control_offset=offset)
        out = ac.tool_result_for_context('tc_hmi_read_smart', {}, result)
        assert len(json.dumps(out, ensure_ascii=False)) <= 6000
        assert len(out['events']) == len(out['controls'])
        assert all(e['trigger'] == 'x' * 200 for e in out['events'])
        seen.extend(c['id'] for c in out['controls'])
        offset = out['next_control_offset']
        if offset is None:
            break
    assert seen == [f'C{i}' for i in range(78)]


def test_smart_source_context_pages_are_lossless(tmp_path):
    from tc_agent import agent_core as ac
    sln, _, _, script = fixture(tmp_path)
    script.write_text('let text = "中文😀";\n' * 1500, encoding='utf-8')
    seen, offset = '', 0
    while True:
        result = idx.read(sln, 'Scripts/deep/a.js', content_offset=offset)
        out = ac.tool_result_for_context('tc_hmi_read_smart', {}, result)
        assert len(json.dumps(out, ensure_ascii=False)) <= 6000
        assert out['source_hash'] == result['source_hash']
        seen += out['content']
        offset = out['next_content_offset']
        if offset is None:
            break
    assert seen == script.read_bytes().decode('utf-8')


def test_mixed_event_source_request_retains_details(tmp_path):
    from tc_agent import agent_core as ac
    sln, _, view, _ = fixture(tmp_path, count=1)
    view.write_text('<div id="B" data-tchmi-type="Button" data-tchmi-trigger="[]"></div>')
    result = idx.read(sln, 'Desktop.view', control_id='B', area='events', include_content=True)
    out = ac.tool_result_for_context('tc_hmi_read_smart', {}, result)
    assert out['events'] == result['events']
    assert out['content'] == result['content']
    assert len(json.dumps(out, ensure_ascii=False)) <= 6000
