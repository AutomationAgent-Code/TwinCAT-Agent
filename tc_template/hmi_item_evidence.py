"""Saved-state evidence for native HMI item creation; never writes or reloads."""
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from .hmi_items import safe_relative


def _key(value):
    return str(value).replace('\\', '/').rstrip('/').casefold()


def inspect_created_item(project_file, relative, kind):
    root = Path(project_file).absolute().parent
    relative, target = safe_relative(root, relative)
    kinds = {'folder': '', 'view': '.view', 'content': '.content',
             'usercontrol': '.usercontrol', 'codebehind_js': '.js',
             'javascript': '.js', 'javascript_classic': '.js', 'css': '.css',
             'function_js': '.js', 'function_js_classic': '.js'}
    if kind not in kinds or (kind != 'folder' and target.suffix.lower() != kinds[kind]):
        raise ValueError('Unsupported kind or mismatched creation output.')
    project = ET.parse(project_file)
    config = json.loads((root / 'Properties/tchmiconfig.json').read_text(encoding='utf-8-sig'))
    checks, detail = {}, {}
    owners = {}
    for node in project.iter():
        if node.get('Include'):
            owners.setdefault(_key(node.get('Include')), []).append(node)

    def registration(rel, tag='Content'):
        nodes = owners.get(_key(rel), [])
        return len(nodes) == 1 and nodes[0].tag.rsplit('}', 1)[-1] == tag

    checks['file_exists'] = target.is_dir() if kind == 'folder' else target.is_file()
    checks['project_registration'] = registration(relative, 'Folder' if kind == 'folder' else 'Content')
    section = {'view': 'views', 'content': 'content', 'usercontrol': 'userControls',
               'function_js': 'userFunctions', 'function_js_classic': 'userFunctions'}.get(kind)
    expected_sections = []
    if section:
        entries = [e for e in config.get(section, []) if _key(e.get('url', '')) == _key(relative)]
        checks['framework_registration'] = len(entries) == 1
        expected_sections.append(section)
    if kind in {'codebehind_js', 'javascript', 'javascript_classic', 'css', 'function_js', 'function_js_classic'}:
        entries = [e for e in config.get('dependencyFiles', []) if _key(e.get('name', '')) == _key(relative)]
        checks['dependency_registration'] = len(entries) == 1 and entries[0].get('type') == (
            'Stylesheet' if kind == 'css' else 'JavaScript')
        expected_sections.append('dependencyFiles')

    if kind in {'usercontrol', 'function_js', 'function_js_classic'}:
        companion = relative + '.json' if kind == 'usercontrol' else str(Path(relative).with_suffix('.function.json')).replace('\\', '/')
        nodes = owners.get(_key(companion), [])
        checks['companion_file'] = (root / companion).is_file()
        checks['companion_registration'] = registration(companion)
        dependencies = [child.text for node in nodes for child in node
                        if child.tag.rsplit('}', 1)[-1] == 'DependentUpon']
        checks['companion_ownership'] = len(dependencies) == 1 and _key(dependencies[0]) in {
            _key(relative), _key(target.name)}
        detail['companion'] = companion
        if checks['companion_file']:
            # Parsing checks syntax only; full installed-schema validation is separate.
            descriptor = json.loads((root / companion).read_text(encoding='utf-8-sig'))
            checks['companion_json_object'] = isinstance(descriptor, dict)
        if kind == 'usercontrol':
            schema_path = root / 'Properties/tchmi.project.Schema.json'
            schema = json.loads(schema_path.read_text(encoding='utf-8-sig')) if schema_path.is_file() else {}
            matches = [name for name, definition in schema.get('definitions', {}).items()
                       if isinstance(definition, dict) and
                       _key(definition.get('frameworkUserControlConfig', '')) == _key(companion)]
            checks['generated_schema_registration'] = bool(matches)
            detail['generated_definitions'] = matches
    detail['expected_config_sections'] = expected_sections
    return {'saved_state_verified': all(checks.values()), 'checks': checks, **detail,
            'failed_checks': [key for key, valid in checks.items() if not valid],
            'source': 'saved-project-config-and-files', 'browser_verified': False,
            'descriptor_schema_verified': False}


def inspect_deleted_item(project_file, files, backup_path):
    """Verify saved deletion AND preservation, without repairing any state.

    The caller must separately verify the live hierarchy and no-reload lifecycle.
    `backup_path` is the actual transaction backup, not a user-selected baseline.
    """
    project = Path(project_file).absolute()
    root, backup = project.parent, Path(backup_path)
    if not files or len(files) != len({_key(f) for f in files}):
        raise ValueError('Deletion evidence requires a unique, nonempty target set.')
    targets = [safe_relative(root, f) for f in files]
    keys = {_key(relative) for relative, _ in targets}
    prior_project = ET.parse(backup / 'project.original')
    saved_project = ET.parse(project)
    prior_config = json.loads((backup / 'config.original').read_text(encoding='utf-8-sig'))
    saved_config = json.loads((root / 'Properties/tchmiconfig.json').read_text(encoding='utf-8-sig'))
    sections = ('views', 'content', 'userControls', 'userFunctions', 'dependencyFiles')

    def strip_config(value):
        result = deepcopy(value)
        for section in sections:
            if section not in result:
                continue
            result[section] = [e for e in result[section] if _key(e.get('url', e.get('name', ''))) not in keys]
            if section in ('views', 'content', 'userControls'):
                # TE2000 sorts page entries and materializes documented false defaults.
                for entry in result[section]:
                    if section in ('views', 'content'):
                        for field in ('preload', 'keepAlive', 'preloadBindings'):
                            entry.setdefault(field, False)
                    if section == 'content':
                        entry.setdefault('loadSync', False)
                result[section].sort(key=lambda e: _key(e.get('url', '')))
        return result

    def registrations(xml):
        def value(node):
            attrs = tuple(sorted((k, _key(v) if k == 'Include' else v) for k, v in node.attrib.items()))
            text = (node.text or '').strip()
            if node.tag.rsplit('}', 1)[-1] == 'DependentUpon':
                text = _key(text)
            return (node.tag, attrs, text, tuple(value(n) for n in node))
        return Counter(value(n) for n in xml.iter() if n.get('Include') and _key(n.get('Include')) not in keys)

    left_config = [section for section in sections if any(
        _key(e.get('url', e.get('name', ''))) in keys for e in saved_config.get(section, []))]
    checks = {
        'files_deleted': not any(path.exists() for _, path in targets),
        'project_registration_removed': not any(_key(n.get('Include')) in keys for n in saved_project.iter() if n.get('Include')),
        'config_registration_removed': not left_config,
        'other_project_registrations_preserved': registrations(prior_project) == registrations(saved_project),
        'other_config_preserved': strip_config(prior_config) == strip_config(saved_config),
    }
    if any(relative.lower().endswith('.usercontrol') for relative, _ in targets):
        # Missing output is NOT evidence that the entire generated schema was cleaned correctly.
        prior_schema_path = backup / 'schema.original'
        saved_schema_path = root / 'Properties/tchmi.project.Schema.json'
        checks['generated_schema_available'] = prior_schema_path.is_file() and saved_schema_path.is_file()
        if checks['generated_schema_available']:
            prior_schema = json.loads(prior_schema_path.read_text(encoding='utf-8-sig'))
            saved_schema = json.loads(saved_schema_path.read_text(encoding='utf-8-sig'))
            expected_schema = deepcopy(prior_schema)
            expected_schema['definitions'] = {name: definition for name, definition in prior_schema.get('definitions', {}).items()
                if not isinstance(definition, dict) or _key(definition.get('frameworkUserControlConfig', '')) not in keys}
            checks['generated_schema_removed'] = not any(isinstance(d, dict) and
                _key(d.get('frameworkUserControlConfig', '')) in keys for d in saved_schema.get('definitions', {}).values())
            checks['other_generated_schema_preserved'] = expected_schema == saved_schema
    return {'saved_state_verified': all(checks.values()), 'checks': checks,
            'failed_checks': [key for key, valid in checks.items() if not valid],
            'source': 'transaction-backup-versus-saved-project', 'live_hierarchy_verified': False,
            'no_reload_verified': False, 'browser_verified': False}
