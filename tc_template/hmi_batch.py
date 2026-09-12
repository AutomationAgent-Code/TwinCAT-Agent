"""Bounded batch input contract, before any XAE access. No global schema cache."""
import re

from .hmi_contract import HmiContractError


def validate_operations(operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise HmiContractError('Provide 1..100 control operations per batch')
    ids = set()
    allowed = {'action', 'control_id', 'control_type', 'parent_id', 'attributes'}
    for index, op in enumerate(operations):
        if not isinstance(op, dict) or set(op) - allowed:
            raise HmiContractError(f'Invalid batch operation fields at index {index}')
        name = op.get('control_id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or name in ids:
            raise HmiContractError(f'Missing, invalid or duplicate control_id at index {index}')
        ids.add(name)
        action = op.get('action')
        if not isinstance(action, str) or action not in {'add', 'update', 'remove'}:
            raise HmiContractError(f'Invalid action for {name}')
        kind, parent = op.get('control_type', ''), op.get('parent_id', '')
        if not isinstance(kind, str) or ((kind or action == 'add') and not kind.startswith('TcHmi.Controls.')):
            raise HmiContractError(f'Full control_type required for {name}')
        if not isinstance(parent, str) or (parent and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', parent)):
            raise HmiContractError(f'Invalid parent_id for {name}')
        attrs = op.get('attributes', {})
        if not isinstance(attrs, dict) or any(
            not isinstance(k, str) or not k.startswith('data-tchmi-') or k == 'data-tchmi-type'
            or (v is not None and not isinstance(v, str)) for k, v in attrs.items()
        ):
            raise HmiContractError(f'Attributes for {name} require data-tchmi-* keys and string/null values')
        if (action != 'add' and parent) or (action == 'remove' and (kind or attrs)):
            raise HmiContractError(f'Ignored fields are not allowed for {action} {name}')


def controls_batch(file, operations, project='', apply=False):
    from ._ps_bridge import ps_com
    return ps_com('hmi-controls-batch', file=file, operations=operations, project=project, apply=apply)
