"""Short-lived rejected drafts; never overwrite an edited page with an old draft."""
from collections import OrderedDict
from pathlib import Path
import threading
import time
import uuid

_drafts = OrderedDict()
_lock = threading.RLock()


def remember(preview):
    from ._ps_bridge import _TOOL_TARGET_PID
    if len(preview.get('markup', '')) > 1_000_000 or not preview.get('source_file'):
        return ''
    key = uuid.uuid4().hex
    with _lock:
        _drafts[key] = (time.monotonic(), _TOOL_TARGET_PID.get(), dict(preview))
        while len(_drafts) > 8: _drafts.popitem(last=False)
    return key


def write_markup(args):
    from ._ps_bridge import ps_com, _TOOL_TARGET_PID
    from .hmi_contract import HmiContractError, digest
    params = dict(args)
    key = params.pop('candidate_id', '')
    changes = params.pop('replacements', [])
    if key:
        if 'markup' in params:
            raise HmiContractError('Use either markup or candidate_id, not both')
        with _lock:
            draft = _drafts.get(key)
        if not draft or time.monotonic() - draft[0] > 1800 or draft[1] != _TOOL_TARGET_PID.get():
            raise HmiContractError('Draft expired or belongs to another XAE; read the target before retrying')
        source = draft[2]
        project_arg = str(params.get('project') or '')
        if project_arg and project_arg.casefold() not in {str(source['project_file']).casefold(), Path(source['project_file']).stem.casefold()}:
            raise HmiContractError('Draft project mismatch')
        if params['file'].replace('\\', '/') != source['file'].replace('\\', '/'):
            raise HmiContractError('Draft file mismatch')
        if digest(Path(source['source_file']).read_text(encoding='utf-8-sig')) != source['source_hash']:
            raise HmiContractError('Page changed since the rejected draft; do not overwrite newer edits')
        if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
            raise HmiContractError('Provide 1..50 exact draft replacements')
        text = source['markup']
        for change in changes:
            old, new, count = change.get('old'), change.get('new'), change.get('count', 1)
            if not isinstance(old, str) or not old or not isinstance(new, str) or type(count) is not int or count < 1:
                raise HmiContractError('Replacement requires nonempty old, string new and positive count')
            if text.count(old) != count:
                raise HmiContractError('Draft replacement occurrence count mismatch; no write performed')
            text = text.replace(old, new)
        params.update(markup=text, project=source['project_file'], _candidate_source_hash=source['source_hash'])
    elif changes or not isinstance(params.get('markup'), str):
        raise HmiContractError('Provide markup, or candidate_id with replacements')
    return ps_com('hmi-write-markup', **params)
