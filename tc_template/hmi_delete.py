"""Conservative, exact-target HMI deletion planning. No recursive folder deletes."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from .hmi_items import safe_relative
from .hmi_paths import HmiPathError, resolve_registered

SECTIONS = ('views', 'content', 'userControls', 'userFunctions', 'dependencyFiles')


def normalized(value):
    return str(value).replace('\\', '/').rstrip('/').casefold()


def plan_delete(project_file, file, repair_orphan=False):
    if repair_orphan:
        raise ValueError('Orphan repair needs a separate offline repair; no-reload deletion requires an existing item.')
    project = Path(project_file).absolute()
    root = project.parent
    relative, target = safe_relative(root, file)
    folder = target.is_dir()
    if folder and any(target.iterdir()):
        raise ValueError('Only empty folders can be deleted.')
    # Deletion is an operation on one persisted project item.  Require one
    # exact Include before doing any reference analysis; a nested same-name
    # item must never satisfy a root-level request.
    try:
        _, target, _ = resolve_registered(project, relative, require_file=False)
    except HmiPathError as exc:
        raise ValueError(str(exc)) from exc
    config_path = root / 'Properties/tchmiconfig.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    doc = ET.parse(project)
    includes = {}
    for node in doc.iter():
        if node.get('Include'):
            key = normalized(node.get('Include'))
            if key in includes and key == normalized(relative):
                raise ValueError('Duplicate project registration: ' + key)
            includes[key] = node
    key = normalized(relative)
    if not folder and target.suffix.lower() not in {'.view', '.content', '.usercontrol', '.js', '.css'}:
        raise ValueError('Unsupported item; descriptor files must be deleted with their owner.')
    for field in ('startupView', 'loginPage'):
        if normalized(config.get(field, '')) == key:
            raise ValueError('Protected ' + field)
    matches = [section for section in SECTIONS if any(
        normalized(entry.get('url', entry.get('name', ''))) == key
        for entry in config.get(section, []))]
    if repair_orphan:
        if target.exists() or key in includes or not matches:
            raise ValueError('Orphan repair requires a missing file/node and a saved config registration.')
    elif not target.exists() or key not in includes:
        raise ValueError('Missing file or project item; not a successful deletion. Use explicit orphan repair if applicable.')
    targets = [relative]
    if not repair_orphan and target.suffix.lower() in {'.usercontrol', '.js'}:
        companion = relative + '.json' if target.suffix.lower() == '.usercontrol' else str(Path(relative).with_suffix('.function.json')).replace('\\', '/')
        companion_path = root / companion
        if companion_path.exists() or normalized(companion) in includes:
            node = includes.get(normalized(companion))
            dependent = next((n.text for n in node if n.tag.rsplit('}', 1)[-1] == 'DependentUpon'), '') if node is not None else ''
            if normalized(dependent) not in {key, normalized(target.name)} or not companion_path.is_file():
                raise ValueError('Companion ownership is ambiguous: ' + companion)
            targets.append(companion)
    target_keys = {normalized(t) for t in targets}
    for target_key in target_keys:
        if sum(normalized(node.get('Include')) == target_key for node in doc.iter() if node.get('Include')) > 1:
            raise ValueError('Duplicate target/companion registration: ' + target_key)
    for include, node in includes.items():
        if include in target_keys:
            continue
        for child in node:
            if child.tag.rsplit('}', 1)[-1] == 'DependentUpon' and normalized(child.text) in {key, normalized(target.name)}:
                raise ValueError('Unexpected dependent item: ' + include)
    for rel in targets:
        # Check ancestors, not just Path.resolve() output: junctions may stay inside root.
        cursor = root
        for part in Path(rel).parts:
            if cursor.is_symlink() or getattr(cursor, 'is_junction', lambda: False)():
                raise ValueError('Reparse point refused.')
            cursor = cursor / part
        if cursor.is_symlink() or getattr(cursor, 'is_junction', lambda: False)():
            raise ValueError('Reparse point refused.')
    after = json.loads(json.dumps(config))
    for section in SECTIONS:
        if section in after:
            after[section] = [entry for entry in after[section] if normalized(entry.get('url', entry.get('name', ''))) not in target_keys]
    needles = {relative.casefold(), target.name.casefold(), target.stem.casefold()}
    remaining_config = json.dumps(after, ensure_ascii=False).replace('\\\\', '/').casefold()
    if any(needle in remaining_config for needle in needles):
        raise ValueError('Other configuration references this item.')
    stamps = []

    def stamp(path):
        stamps.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    stamp(project)
    stamp(config_path)
    for include, node in includes.items():
        original = node.get('Include').replace('\\', '/').rstrip('/')
        source = root / original
        if source.resolve() == config_path.resolve():
            continue  # Already reviewed after removing only the planned registrations.
        if source.is_file():
            if not source.resolve().is_relative_to(root.resolve()):
                raise ValueError('External registered files require manual review.')
            if include in target_keys:
                stamp(source)
            elif source.suffix.lower() in {'.view', '.content', '.usercontrol', '.js', '.ts', '.json', '.css', '.html'}:
                text = source.read_text(encoding='utf-8-sig').replace('\\', '/').casefold()
                if normalized(original) == 'properties/tchmi.project.schema.json' and target.suffix.lower() == '.usercontrol':
                    generated = json.loads(source.read_text(encoding='utf-8-sig'))
                    generated['definitions'] = {name: value for name, value in generated.get('definitions', {}).items()
                        if normalized(value.get('frameworkUserControlConfig', '')) not in target_keys}
                    # Exclude only this UC's generated definition, not real references from other definitions.
                    text = json.dumps(generated, ensure_ascii=False).replace('\\\\', '/').casefold()
                if any(needle in text for needle in needles):
                    raise ValueError('Referenced by: ' + original)
                stamp(source)
    return dict(project_file=str(project), relative=relative, files=targets, folder=folder,
                repair_orphan=bool(repair_orphan), stamps=stamps, config_sections=matches,
                reload_required=False, api='IVsHierarchyDeleteHandler3.DeleteItems', written=False,
                status='preview', verified=False, apply_required=True)


def delete_item(file, project='', apply=False, repair_orphan=False):
    from . import _ps_bridge as ps
    from .hmi_contract import Catalog, HmiContractError
    info = ps.com_hmi_project_info(project)
    if Catalog(Path(info['project_file'])).framework != 'native1.12-tchmi':
        raise HmiContractError('Native deletion is verified only for native1.12-tchmi.')
    plan = plan_delete(info['project_file'], file, repair_orphan)
    if not apply:
        return {k: v for k, v in plan.items() if k != 'stamps'}
    result = ps.ps_com('hmi-item-delete', project=info['project_file'], plan=plan, timeout=90)
    if result.get('verified') is True:
        try:
            from .hmi_item_evidence import inspect_deleted_item
            evidence = inspect_deleted_item(info['project_file'], plan['files'], result['backup_path'])
            result['saved_state_evidence'] = evidence
            if not evidence['saved_state_verified']:
                result.update(status='incomplete', verified=False, retry_safe=False,
                              error='Deletion synchronization/preservation incomplete: ' + ', '.join(evidence['failed_checks']))
        except Exception as exc:
            result.update(status='incomplete', verified=False, retry_safe=False,
                          error='Cannot verify deletion synchronization/preservation: ' + str(exc))
        # Verification is read-only. Never auto-reload or attempt a second deletion on failure.
    return result
