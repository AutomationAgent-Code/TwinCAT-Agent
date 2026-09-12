"""Fail-closed live baselines for structural PLC member edits.

Reads strict COM properties: failed enumeration/text access is never empty code.
No Save, silent mode, compiler or runtime operations are used here.
"""
from copy import deepcopy
import hashlib
import json
import re


def capture(dte, parent, path, *, document_save=False):
    count = 0

    def node(item, depth=0):
        nonlocal count
        count += 1
        if depth > 32 or count > 1000:
            raise ValueError('Member baseline exceeds safe tree limits')
        kind, name = int(item.ItemType), str(item.Name)
        areas = {
            601: (), 602: ('DeclarationText', 'ImplementationText'),
            603: ('DeclarationText', 'ImplementationText'),
            604: ('DeclarationText', 'ImplementationText'),
            618: ('DeclarationText',), 608: ('ImplementationText',),
            609: ('DeclarationText', 'ImplementationText'), 610: ('DeclarationText',),
            611: ('DeclarationText',), 612: ('DeclarationText',),
            613: ('DeclarationText', 'ImplementationText'),
            614: ('DeclarationText', 'ImplementationText'),
            616: ('ImplementationText',),
        }
        if document_save:
            areas.update({k: ('DeclarationText',) for k in (605, 606, 607, 615, 623)})
        if kind not in areas:
            # In particular, never probe unsafe interface accessor text (type 0).
            raise ValueError(f'Unsupported member baseline node: {name} (type {kind})')
        result = {'name': name, 'itemType': kind, 'source': {}, 'children': []}
        for area in areas[kind]:
            value = getattr(item, area)
            value = value() if callable(value) else value
            if not isinstance(value, str):
                raise ValueError(f'Missing live text: {name}.{area}')
            if document_save:
                value = value.replace('\r\n', '\n').replace('\r', '\n')
            result['source'][area] = hashlib.sha256(value.encode('utf-8')).hexdigest()
        result['children'] = sorted([node(child, depth + 1) for child in list(item)], key=lambda n: (n['name'], n['itemType']))
        return result

    def read():
        return {'version': 1, 'path': path, 'solution': str(dte.Solution.FullName),
                'window': int(dte.MainWindow.HWnd), 'tree': node(parent)}

    first = read()
    count = 0
    second = read()
    if first != second:
        raise ValueError('PLC member tree changed while collecting baseline')
    token = {k: first[k] for k in ('version', 'path', 'solution', 'window')}
    token['sha256'] = hashlib.sha256(json.dumps(first, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return token, first['tree']


def expected_tree(tree, member, new_name=None, declaration=None):
    result = deepcopy(tree)
    parent = result
    parts = member.split('.')
    for part in parts[:-1]:
        parent = next(n for n in parent['children'] if n['name'].casefold() == part.casefold())
    target = next(n for n in parent['children'] if n['name'].casefold() == parts[-1].casefold())
    if new_name is None:
        parent['children'].remove(target)
    else:
        target['name'] = new_name
        if declaration is not None:
            target['source']['DeclarationText'] = hashlib.sha256(declaration.encode('utf-8')).hexdigest()
        parent['children'].sort(key=lambda n: (n['name'], n['itemType']))
    return result


def renamed_declaration(source, old, new):
    # Model only the definition identifier edit, not arbitrary references/body.
    return re.sub(r'(?im)^((?:METHOD|PROPERTY|ACTION|TRANSITION)\b[^:\r\n]*?\b)' + re.escape(old.split('.')[-1]) + r'\b',
                  lambda m: m[1] + new, source, count=1)


def conflict(reason, *, written=False):
    return {'status': 'conflict' if not written else 'uncertain',
            'error_type': 'member_baseline', 'condition': 'source_conflict',
            'written': written, 'not_executed': not written, 'verified': False,
            'retry_safe': False, 'error': str(reason),
            'next_action': '用 plc_read(name=所属POU,path=精确路径,structure_baseline=true) 重新读取成员基线；不自动保存或重放。'}
