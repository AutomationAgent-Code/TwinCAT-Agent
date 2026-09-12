"""Exact, revision-guarded PLC document saves. Never SaveAll or reopen XAE."""
import hashlib
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .member_baseline import capture


def _key(path):
    return str(Path(path).resolve()).casefold()


def _source_file(system_file, tree_path):
    parts = tree_path.split('^')
    if len(parts) < 4 or parts[0] != 'TIPC':
        raise ValueError('An exact PLC object tree path is required')
    system_file = Path(system_file).resolve()
    if system_file.suffix.lower() != '.tsproj':
        raise ValueError('Cannot verify the System Manager project file')
    projects = [p for p in ET.parse(system_file).getroot().iter()
                if p.get('Name') == parts[1] and p.get('PrjFilePath')]
    if len(projects) != 1:
        raise ValueError('PLC project mapping is missing or ambiguous; no save performed')
    project = (system_file.parent / unquote(projects[0].get('PrjFilePath')).replace('\\', '/')).resolve()
    matches = []
    for item in ET.parse(project).getroot().iter():
        if item.tag.rsplit('}', 1)[-1] != 'Compile' or not item.get('Include'):
            continue
        include = unquote(item.get('Include')).replace('\\', '/')
        # Linked/logically relocated objects are not guessed from their basename.
        logical = next((c.text for c in item if c.tag.rsplit('}', 1)[-1] == 'Link'), None) or include
        logical = str(Path(logical.replace('\\', '/')).with_suffix('')).replace('\\', '^').replace('/', '^')
        if logical.casefold() == '^'.join(parts[3:]).casefold():
            matches.append((project.parent / include).resolve())
    if len(matches) != 1 or matches[0].suffix.lower() not in {'.tcpou', '.tcgvl', '.tcdut', '.tcio'}:
        raise ValueError('Cannot bind the PLC tree to one registered source document')
    return matches[0]


def _document(dte, file):
    matches = []
    for doc in list(dte.Documents):
        # Member editors may expose File.TcPOU@Method. Saving must target the
        # parent document, not silently substitute a member editor.
        if _key(str(doc.FullName)) == _key(file):
            matches.append(doc)
    if len(matches) != 1:
        raise ValueError('Open the exact parent PLC document in XAE first (not only a member editor)')
    return matches[0]


def _disk_tree(file, live_tree):
    root = ET.parse(file).getroot()
    objects = [n for n in root if n.get('Name') == live_tree['name']]
    if len(objects) != 1:
        raise ValueError('Saved XML does not contain the expected PLC object')
    def visit(xml, live):
        tags = {602: 'POU', 603: 'POU', 604: 'POU', 605: 'DUT', 606: 'DUT',
                607: 'DUT', 623: 'DUT', 615: 'GVL', 618: 'Itf', 601: 'Folder',
                608: 'Action', 609: 'Method', 610: 'Method', 611: 'Property',
                612: 'Property', 613: 'Get', 614: 'Set', 616: 'Transition'}
        if xml.tag != tags.get(live['itemType']):
            raise ValueError('Saved member type differs from the live tree')
        result = {'name': live['name'], 'itemType': live['itemType'], 'source': {}, 'children': []}
        for prop in live['source']:
            tag = 'Declaration' if prop == 'DeclarationText' else 'Implementation'
            element = xml.find(tag)
            if tag == 'Implementation' and element is not None:
                if list(element) and element.find('ST') is None:
                    raise ValueError('Non-ST document verification is not supported')
                element = element.find('ST')
            value = element.text or '' if element is not None else ''
            result['source'][prop] = hashlib.sha256(value.encode('utf-8')).hexdigest()
        children = [n for n in xml if n.tag in {'Method', 'Property', 'Action', 'Transition', 'Get', 'Set', 'Folder'}]
        if len(children) != len(live['children']):
            raise ValueError('Saved member count differs from the live tree')
        for child in live['children']:
            found = [n for n in children if (n.get('Name') or n.tag) == child['name']]
            if len(found) != 1:
                raise ValueError('Saved member identity mismatch')
            result['children'].append(visit(found[0], child))
        return result
    return visit(objects[0], live_tree)


def execute(dte, parent, path, system_file, expected=None, *, save=False):
    attempted = False
    try:
        file = _source_file(system_file, path)
        doc = _document(dte, file)
        def snapshot():
            if _source_file(system_file, path) != file or _key(doc.FullName) != _key(file):
                raise ValueError('Document identity changed')
            baseline, tree = capture(dte, parent, path, document_save=True)
            saved = doc.Saved
            if saved not in (True, False, 0, -1):
                raise ValueError('Document saved state is unavailable')
            return {'member': baseline, 'file': str(file), 'saved': bool(saved),
                    'disk_sha256': hashlib.sha256(file.read_bytes()).hexdigest()}, tree
        before, tree = snapshot()
        if any(list(n) and n.find('ST') is None for n in ET.parse(file).getroot().iter('Implementation')):
            raise ValueError('Only ST source documents are supported')
        if snapshot()[0] != before:
            raise ValueError('Document changed while reading save baseline')
        if not save:
            return {'status': 'read', 'document_baseline': before, 'file': str(file), 'saved': before['saved'],
                    'disk_live_match': _disk_tree(file, tree) == tree if before['saved'] else None}
        if expected != before:
            raise ValueError('Save baseline expired; re-read and request approval again')
        if not before['saved']:
            attempted = True
            # EnvDTE.Document.Save, exact existing filename, no SaveAs/SaveAll.
            status = doc.Save(str(file))
            if status != 0:
                raise ValueError(f'XAE did not confirm document save (status {status})')
        after, after_tree = snapshot()
        if not after['saved'] or after['member'] != before['member']:
            raise ValueError('Live source changed during save or remains unsaved')
        if _disk_tree(file, after_tree) != after_tree or snapshot()[0] != after:
            raise ValueError('Saved XML and live source are not confirmed identical')
        return {'status': 'saved' if attempted else 'already_saved', 'verified': True,
                'saved': True, 'written': attempted, 'file': str(file), 'document_baseline': after,
                'verification': 'disk_live_source_and_members_match', 'compiled': False}
    except Exception as exc:
        return {'status': 'uncertain' if attempted else 'conflict', 'error_type': 'document_save',
                'error': str(exc), 'written': 'unknown' if attempted else False,
                'not_executed': not attempted, 'verified': False, 'retry_safe': False,
                'next_action': '重新 plc_read(document_baseline=true)；保存须重新按次审批，不重试保存、不关闭工程。'}
