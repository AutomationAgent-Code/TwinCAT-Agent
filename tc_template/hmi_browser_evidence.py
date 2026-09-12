"""Compare saved startup controls to the actual browser DOM, not just root presence."""
import json
from pathlib import Path
from .hmi_contract import _xml


def compare_controls(result, project_file):
    project = Path(project_file).resolve()
    config = json.loads((project.parent / 'Properties/tchmiconfig.json').read_text(encoding='utf-8-sig'))
    requested = str(result.get('requested_entry_page') or config['startupView']).replace('\\', '/')
    target = (project.parent / requested).resolve()
    if not target.is_relative_to(project.parent):
        raise ValueError('Target view escapes project root')
    if not target.is_file():
        raise ValueError('Saved target view does not exist: ' + requested)
    expected_root = _xml(target.read_text(encoding='utf-8-sig'))
    expected = {n.get('id') for n in expected_root.iter() if n.get('data-tchmi-type') and n.get('id')}
    binding_count = sum(1 for node in expected_root.iter() for value in node.attrib.values()
                        if isinstance(value, str) and any(tag in value for tag in ('%s%', '%i%', '%ctrl%')))
    relative = target.relative_to(project.parent).as_posix()
    result['requested_entry_page'] = result.get('requested_entry_page') or relative
    if 'loaded_view' not in result:
        result['loaded_view'] = relative
    result['saved_target_page'] = relative
    result['saved_target_page_exists'] = True
    result['saved_target_control_count'] = len(expected)
    result['saved_target_binding_count'] = binding_count
    result['expected_startup_control_count'] = len(expected)
    result['controls_verified'] = bool(expected) and bool(result.get('viewport_results'))
    for viewport in result.get('viewport_results', []):
        actual = set((viewport.get('metrics') or {}).get('control_ids', []))
        missing = sorted(expected - actual)
        viewport['missing_saved_control_ids'] = missing
        viewport['saved_controls_verified'] = bool(expected) and not missing
        viewport['valid'] = bool(viewport.get('valid')) and viewport['saved_controls_verified']
        result['controls_verified'] &= viewport['saved_controls_verified']
    if not result['controls_verified']:
        result.update(status='failed', success=False)
    result['saved_target_page_verified'] = result['controls_verified']
    return result
