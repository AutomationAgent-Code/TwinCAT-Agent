"""Project-version-specific HMI control events; no PLC writes during editing."""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET


ACTION_SCHEMA = {
    'type': 'object', 'required': ['objectType'],
    'properties': {
        'objectType': {'type': 'string', 'enum': ['JavaScript', 'WriteToSymbol', 'Function', 'ControlApiFunction']},
        'symbolExpression': {'type': 'string', 'description': 'WriteToSymbol 必填，例如 %s%ADS.PLC1.GVL_Hmi.bStart%/s%'},
        'value': {'type': 'object', 'required': ['objectType'], 'properties': {
            'objectType': {'type': 'string', 'enum': ['StaticValue', 'Symbol', 'EventDataObject']},
            'value': {'description': 'StaticValue 必填：实际 JSON 值（例如 true），不是字符串化的 JSON'},
            'symbolExpression': {'type': 'string'}, 'propertyPath': {'type': 'string'}}},
        'sourceLines': {'type': 'array', 'items': {'type': 'string'}, 'description': 'JavaScript 必填'},
        'fn': {'type': 'string', 'description': 'Function/ControlApiFunction 必填'},
        'fnParams': {'type': 'array', 'items': {'type': 'object'}, 'description': 'Function 必填'},
        'control': {'type': 'string', 'description': 'ControlApiFunction 必填'},
    },
}
for _branch in ('success', 'error'):
    ACTION_SCHEMA['properties'][_branch] = {'type': 'array', 'items': {'type': 'object'},
        'description': '可选回调动作数组，每项遵循相同 objectType 动作结构'}


def action_contract():
    return {'discriminator': 'objectType', 'schema': ACTION_SCHEMA,
            'write_example': {'objectType': 'WriteToSymbol',
                'symbolExpression': '%s%ADS.PLC1.GVL_Hmi.bStart%/s%',
                'value': {'objectType': 'StaticValue', 'value': True}},
             'default_placement': 'native',
             'placement_values': ['native', 'custom'],
             'note': ('示例符号必须替换成当前项目真实映射；先 read 获取实际事件，再 apply=false 预览。'
                      '常规事件传入 .onPressed 等短名称并使用 placement=native；序列化格式按安装版本解析，'
                      '支持 owner 上下文的版本保存为 %ctx%owner::Id|EventRegistrationMode=Resolve%/ctx%.onPressed，'
                      '显示在 XAE 下方原生事件组；'
                      '只有明确需要上方 Custom 事件时才使用 placement=custom。')}


def recommended_events(events: list[dict]) -> list[str]:
    """Return common pointer/touch events, but only when actually installed."""
    names = [str(item.get('name') or '') for item in events]
    preferred = ('.onPressed', '.onStatePressed', '.onStateReleased', '.onMouseDown', '.onMouseUp',
                 '.onTapped', '.onClicked')
    selected = [name for name in preferred if name in names]
    return selected or names[:6]


class HmiEventValidationError(ValueError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = {'error_type': 'hmi_event_validation', 'action_contract': action_contract(), **details}


OWNER_EVENT_PREFIX = '%ctx%owner::Id|EventRegistrationMode=Resolve%/ctx%'


def validate_hmi_file(file, project_file: str | None = None):
    if not isinstance(file, str) or not file.strip():
        raise ValueError('file 必须是有效项目文件路径，例如 Desktop.view')
    # Reject protocol fragments/control characters before any COM call. Do not
    # guess a corrected file name or execute a sanitized version of user input.
    tail = file[2:] if re.match(r'^[A-Za-z]:[\\/]', file) else file
    if any(ord(c) < 32 for c in file) or any(c in tail for c in '<>"|?*:'):
        raise ValueError('file 含非法路径字符或工具协议片段；请重新提供实际文件路径，例如 Desktop.view')
    if project_file and str(project_file).lower().endswith('.hmiproj'):
        # When the caller supplied an explicit project path, close the path
        # contract before entering COM.  Auto-selected projects are resolved
        # by the PowerShell bridge, where the open XAE solution is authoritative.
        from .hmi_paths import HmiPathError, resolve_registered
        try:
            resolve_registered(project_file, file)
        except HmiPathError as exc:
            raise ValueError(str(exc)) from exc


def catalog(project_file: str) -> dict:
    # Share exact recorded package-root resolution with the markup gate.
    # HMI projects may live outside their solution's directory.
    from .hmi_contract import Catalog
    installed = Catalog(project_file)
    descriptions = {name: (data, installed.control_paths[name])
                    for name, data in installed.controls.items()}
    result = {}
    def resolve(name, visiting):
        if name in visiting:
            raise ValueError('Cyclic control inheritance: ' + name)
        if name not in descriptions:
            raise ValueError('Missing control description: ' + name)
        data, _ = descriptions[name]
        events = {}
        if data.get('base'):
            events.update(resolve(data['base'], visiting | {name}))
        for event in data.get('events', []):
            events[event['name']] = dict(event, declared_by=name)
        return events
    for name, (_, file) in descriptions.items():
        result[name] = {'description_file': str(file), 'events': list(resolve(name, set()).values())}
    native_prefix = ''
    base_path = installed.control_paths.get('TcHmi.Controls.System.TcHmiControl')
    if base_path:
        runtime = next((p for p in base_path.parents if p.name == installed.framework), None)
        implementation = runtime / 'dist/Controls/System/TcHmiControl/TcHmiControl.esm.js' if runtime else None
        if implementation and implementation.is_file() and OWNER_EVENT_PREFIX in implementation.read_text(encoding='utf-8-sig'):
            native_prefix = OWNER_EVENT_PREFIX
    return {'framework': installed.framework, 'controls': result, 'native_event_prefix': native_prefix,
            'native_event_format_source': 'installed-control-implementation' if native_prefix else 'legacy-short-name'}


def read_events(markup: str, control_id: str) -> tuple[str, list]:
    nodes = [n for n in ET.fromstring(markup).iter() if n.get('id') == control_id]
    if len(nodes) != 1:
        raise ValueError('Control must exist uniquely: ' + control_id)
    node = nodes[0]
    script = [n for n in node if n.get('data-tchmi-target-attribute') == 'data-tchmi-trigger']
    if script:
        raise ValueError('Script-backed Trigger requires explicit migration; existing events were preserved')
    triggers = json.loads(node.get('data-tchmi-trigger', '[]'))
    if not isinstance(triggers, list):
        raise ValueError('Trigger must be an array')
    return node.get('data-tchmi-type', ''), triggers


def validate_actions(actions: list) -> None:
    if not isinstance(actions, list) or not actions:
        raise ValueError('At least one event action is required')
    required = {'JavaScript': ('sourceLines',), 'WriteToSymbol': ('symbolExpression', 'value'),
                'Function': ('fn', 'fnParams'), 'ControlApiFunction': ('control', 'fn')}
    for action in actions:
        kind = action.get('objectType') if isinstance(action, dict) else None
        if kind not in required:
            raise HmiEventValidationError('动作缺少或使用了不支持的 objectType；不要使用 actionType/type/action 作为动作类型字段。')
        if any(k not in action for k in required[kind]):
            raise HmiEventValidationError('Missing required action fields: ' + kind,
                                          required_fields=list(required[kind]))
        if kind == 'JavaScript' and (not isinstance(action['sourceLines'], list) or
                not all(isinstance(s, str) for s in action['sourceLines'])):
            raise ValueError('sourceLines must be a string array')
        if kind == 'WriteToSymbol':
            if not isinstance(action['symbolExpression'], str) or not re.fullmatch(r'%(s|i|ctrl)%.+%/\1%', action['symbolExpression']):
                raise HmiEventValidationError('Invalid target symbol expression')
            from .hmi_symbols import check_symbol_value
            check_symbol_value(action['symbolExpression'])
            value = action['value']
            if not isinstance(value, dict) or value.get('objectType') not in ('StaticValue', 'Symbol', 'EventDataObject'):
                raise HmiEventValidationError('Invalid action value; use {objectType:StaticValue,value:...}')
            key = {'StaticValue': 'value', 'Symbol': 'symbolExpression', 'EventDataObject': 'propertyPath'}[value['objectType']]
            if key not in value:
                raise ValueError('Missing action value: ' + key)
        for branch in ('success', 'error'):
            if action.get(branch):
                validate_actions(action[branch])


def plan_event(markup: str, control_id: str, event: str, actions: list,
               control_catalog: dict, action: str = 'upsert',
               placement: str = 'native') -> list:
    control_type, triggers = read_events(markup, control_id)
    if action not in ('upsert', 'remove'):
        raise ValueError('action must be upsert or remove')
    if placement not in ('native', 'custom'):
        raise HmiEventValidationError('placement must be native or custom')
    short = '.' + event.split('.')[-1]
    prefix = control_catalog.get('native_event_prefix', '')
    if event not in (short, short[1:], control_id + short, prefix + short):
        raise ValueError('Event belongs to a different control')
    available = control_catalog['controls'].get(control_type, {}).get('events', [])
    if short not in {e['name'] for e in available}:
        raise HmiEventValidationError(f'Event {short} is not declared for {control_type}',
                                      available_events=[e['name'] for e in available],
                                      recommended_events=recommended_events(available))
    full = control_id + short
    desired = prefix + short if placement == 'native' else full
    # Upsert may migrate the old control-qualified representation to the
    # declared native event.  Remove remains exact so a native removal cannot
    # silently delete an explicitly requested Custom trigger (or vice versa).
    aliases = (short, full, prefix + short) if action == 'upsert' else (desired,)
    matches = [t for t in triggers if t.get('event') in aliases]
    if len(matches) > 1:
        raise ValueError('Duplicate event handlers; refusing ambiguous replacement')
    if action == 'remove' and not matches:
        raise ValueError('Event handler does not exist')
    result = [t for t in triggers if t not in matches]
    if action == 'upsert':
        validate_actions(actions)
        handler = dict(matches[0]) if matches else {}
        handler['event'] = desired
        handler['actions'] = actions
        result.append(handler)
    return result


def control_events(file: str, control_id: str, project: str = '', event: str = '',
                   actions: list | None = None, action: str = 'read', apply: bool = False,
                   placement: str = 'native') -> dict:
    from . import _ps_bridge as ps
    validate_hmi_file(file)
    if action not in ('read', 'upsert', 'remove'):
        raise HmiEventValidationError('action must be read, upsert or remove')
    if placement not in ('native', 'custom'):
        raise HmiEventValidationError('placement must be native or custom')
    if action == 'upsert':
        validate_actions(actions or [])
    info = ps.com_hmi_project_info(project)
    exact_project = info['project_file']
    data = ps.com_hmi_read(file, project=exact_project, max_chars=1000000)
    if data.get('truncated'):
        raise ValueError('Markup is truncated; refusing event edit')
    control_type, current = read_events(data['content'], control_id)
    installed = catalog(info['project_file'])
    available = installed['controls'].get(control_type)
    if available is None:
        raise ValueError('Installed description not found: ' + control_type)
    if action == 'read':
        native_names = {str(item.get('name') or '') for item in available['events']}
        placements = []
        for item in current:
            stored = str(item.get('event') or '')
            native_equivalent = '.' + stored.split('.')[-1] if '.' in stored else ''
            prefix = installed.get('native_event_prefix', '')
            is_native = stored.startswith(prefix) if prefix else stored.startswith('.')
            misplaced = (native_equivalent in native_names and stored != prefix + native_equivalent)
            placements.append({
                'event': stored,
                'placement': ('native' if is_native else ('legacy-short' if stored.startswith('.') else 'custom')),
                'native_equivalent': native_equivalent if misplaced else '',
                'migration_recommended': misplaced,
            })
        return dict(status='read', control_id=control_id, control_type=control_type,
                    triggers=current, framework=installed['framework'],
                    trigger_placements=placements, default_placement='native',
                    placement_note=('常规事件写入下方原生事件组；只有明确的特殊需求才使用上方 Custom。'),
                    action_contract=action_contract(),
                    native_event_prefix=installed.get('native_event_prefix', ''),
                    available_events=[item['name'] for item in available['events']],
                    recommended_events=recommended_events(available['events']), **available)
    planned = plan_event(data['content'], control_id, event, actions or [], installed, action, placement)
    result = ps.com_hmi_control_edit(file, 'update', control_id, project=exact_project,
        attributes={'data-tchmi-trigger': json.dumps(planned, ensure_ascii=False)}, apply=apply,
        _event_placement=placement)
    if apply:
        _, actual = read_events(ps.com_hmi_read(file, project=exact_project)['content'], control_id)
        if actual != planned:
            raise RuntimeError('Event readback does not match requested triggers')
    return dict(result, triggers=planned, placement=placement, runtime_tested=False)


def control_events_batch(file: str, edits: list, project: str = '', apply: bool = False) -> dict:
    """Dedicated native-event batch: validate every handler, save/read back once.

    This is not a generic Trigger bypass. No custom placement or arbitrary
    control operations are accepted; the existing guarded batch performs writes.
    """
    from . import _ps_bridge as ps
    from .hmi_contract import digest
    validate_hmi_file(file)
    if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
        raise HmiEventValidationError('Provide 1..100 native event edits')
    pairs = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'control_id', 'event', 'actions'}:
            raise HmiEventValidationError('Each edit requires control_id, event and actions only')
        if not isinstance(edit['control_id'], str) or not isinstance(edit['event'], str):
            raise HmiEventValidationError('Control and event names must be strings')
        pair = (edit['control_id'], '.' + edit['event'].split('.')[-1])
        if pair in pairs:
            raise HmiEventValidationError('Duplicate control/event pair')
        pairs.add(pair)
        validate_actions(edit['actions'])
    info = ps.com_hmi_project_info(project)
    exact = info['project_file']
    source = ps.com_hmi_read(file, project=exact, max_chars=1000000)
    if source.get('truncated'):
        raise HmiEventValidationError('Page truncated; no event was changed')
    installed = catalog(exact)
    root = ET.fromstring(source['content'])
    planned = {}
    for edit in edits:
        markup = ET.tostring(root, encoding='unicode')
        triggers = plan_event(markup, edit['control_id'], edit['event'], edit['actions'], installed)
        node = next(n for n in root.iter() if n.get('id') == edit['control_id'])
        node.set('data-tchmi-trigger', json.dumps(triggers, ensure_ascii=False))
        planned[edit['control_id']] = triggers
    operations = [{'action': 'update', 'control_id': name,
                   'attributes': {'data-tchmi-trigger': json.dumps(triggers, ensure_ascii=False)}}
                  for name, triggers in planned.items()]
    result = ps.ps_com('hmi-controls-batch', project=exact, file=file, operations=operations,
                       apply=apply, _event_placement='native',
                       _candidate_source_hash=digest(source['content']))
    if apply:
        actual = ps.com_hmi_read(file, project=exact, max_chars=1000000)
        if actual.get('truncated') or any(read_events(actual['content'], name)[1] != triggers
                                         for name, triggers in planned.items()):
            raise RuntimeError('Native event batch readback mismatch')
    return dict(result, event_count=len(edits), placement='native', runtime_tested=False)
