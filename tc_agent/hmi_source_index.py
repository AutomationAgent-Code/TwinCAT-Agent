"""Project-scoped incremental HMI source/control index; saved files only.

No editor/ADS/Server calls. A task must bind an exact solution before entering.
The database is disposable acceleration data, never an authority for dirty XAE.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from tc_agent.plc_cache import CACHE
from tc_template.hmi_paths import HmiPathError

_LOCK = threading.RLock()
_MANIFESTS: OrderedDict = OrderedDict()
_SUFFIXES = {'.view', '.content', '.usercontrol', '.partial', '.function', '.js', '.ts',
             '.css', '.json', '.localization', '.theme', '.config'}
_MARKUP = {'.view', '.content', '.usercontrol'}
_EXCLUDE = {'bin', 'obj', 'packages', '.git', '.twincatagent', '.engineering_servers', '.updates'}
_MAX_BYTES = 8 * 1024 * 1024
_INDEX_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS hmi_files (
 project_file TEXT NOT NULL, file TEXT NOT NULL, relative_path TEXT NOT NULL,
 mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, epoch INTEGER NOT NULL,
 digest TEXT NOT NULL, content TEXT NOT NULL, parse_error TEXT NOT NULL,
 PRIMARY KEY(project_file,file));
CREATE TABLE IF NOT EXISTS hmi_controls (
 project_file TEXT NOT NULL, file TEXT NOT NULL, ordinal INTEGER NOT NULL,
 control_id TEXT NOT NULL, type TEXT NOT NULL, attributes TEXT NOT NULL,
 PRIMARY KEY(project_file,file,ordinal));
CREATE INDEX IF NOT EXISTS ix_hmi_control_id ON hmi_controls(project_file,control_id);
'''


def _stamp(path: Path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _stable_text(path: Path) -> tuple[str, tuple]:
    before = _stamp(path)
    if before[1] > _MAX_BYTES:
        raise ValueError(f'HMI file exceeds {_MAX_BYTES} byte index limit: {path.name}')
    raw = path.read_bytes()
    after = _stamp(path)
    if before != after or len(raw) != after[1]:
        raise ValueError(f'HMI file changed during read; retry: {path.name}')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        text = raw.decode('utf-8-sig')
    return text, after


def _xml(text: str):
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', text, re.I):
        raise ValueError('DTD/entity declarations are not supported in the HMI source index')
    return ET.fromstring(text)


def _normalize_index_relative(relative: str) -> str:
    """Normalize a saved TE2000 Include for read-only indexing.

    ``hmi_paths.normalize_relative`` is intentionally strict because it also
    protects write operations.  TE2000's generated project can legitimately
    register names such as ``.gitignore`` and
    ``Numpad (plusminus).keyboard.json``.  The index must recognize those
    files without weakening the write-path contract, so it has a separate
    read-only normalizer with the same traversal/root/escape protections.
    """
    value = str(relative or '').replace('\\', '/')
    if not value or value.startswith('/') or re.match(r'^[A-Za-z]:', value):
        raise HmiPathError('HMI file path must be relative to the project.')
    if any(ord(char) < 32 for char in value) or any(char in value for char in '<>"|?*:'):
        raise HmiPathError('HMI file path contains illegal characters.')
    parts = value.split('/')
    if any(not part or part in {'.', '..'} for part in parts):
        raise HmiPathError('HMI file path cannot contain empty or traversal segments.')
    for part in parts:
        if part != part.strip() or part.endswith('.'):
            raise HmiPathError('HMI file path contains an invalid segment.')
        if part.upper().split('.', 1)[0] in _INDEX_RESERVED_NAMES:
            raise HmiPathError('HMI file path contains a reserved device name.')
    return '/'.join(parts)


def _manifest(solution: str) -> tuple[Path, list[dict]]:
    if not solution or Path(solution).suffix.casefold() != '.sln':
        raise ValueError('An exact saved XAE .sln is required for the HMI index')
    sln = Path(solution).resolve()
    stamp = _stamp(sln)
    key = str(sln)
    cached = _MANIFESTS.get(key)
    if cached and cached[0] == stamp and cached[1] == CACHE.revision:
        try:
            if all(_stamp(p['path']) == p['stamp'] for p in cached[2]):
                _MANIFESTS.move_to_end(key)
                return sln, cached[2]
        except OSError:
            pass
    text, stamp = _stable_text(sln)
    refs = re.findall(r'^Project\([^\n]+?\)\s*=\s*"([^"]+)",\s*"([^"]+)"', text, re.M)
    projects = []
    for name, reference in refs:
        path = (sln.parent / unquote(reference).replace('\\', '/')).resolve()
        if path.suffix.casefold() != '.hmiproj':
            continue
        source, project_stamp = _stable_text(path)
        root = _xml(source)
        files, issues = {}, []
        for node in root.iter():
            if node.tag.rsplit('}', 1)[-1] not in {'Content', 'Compile', 'None'}:
                continue
            include = unquote(str(node.get('Include') or '')).replace('\\', '/')
            if not include:
                continue
            if any(token in include for token in ('*', '?', '$(', '@(')):
                issues.append({'include': include, 'reason': 'MSBuild expression/glob not evaluated'})
                continue
            # Filter non-indexable MSBuild items before path validation.  The
            # solution commonly contains .gitignore/.tfignore and build
            # outputs; they are not HMI source and should not make an
            # otherwise usable index incomplete.
            raw_relative = Path(include)
            if raw_relative.suffix.casefold() not in _SUFFIXES or any(
                    p.casefold() in _EXCLUDE for p in raw_relative.parts):
                continue
            try:
                canonical = _normalize_index_relative(include)
            except HmiPathError as exc:
                issues.append({'include': include, 'reason': str(exc)})
                continue
            relative = Path(canonical)
            file = (path.parent / relative).resolve()
            if not file.is_relative_to(path.parent):
                issues.append({'include': include, 'reason': 'Linked file escapes HMI project root'})
                continue
            file_key = str(file).casefold()
            if file_key in files:
                # TE2000 1.12/1.14 may emit the same Content Include in both
                # generated project sections.  It resolves to one physical
                # file, so indexing it twice adds no information and would
                # make large projects falsely report incomplete.  Keep the
                # first canonical entry and retain a count for diagnostics.
                files[file_key]['duplicate_registrations'] = (
                    files[file_key].get('duplicate_registrations', 0) + 1)
                continue
            files[file_key] = {'file': file, 'relative': canonical,
                               'duplicate_registrations': 0}
        projects.append({'name': name, 'path': path, 'stamp': project_stamp,
                         'files': files, 'issues': issues})
    if not projects:
        raise ValueError('No saved HMI project reference exists in the selected solution')
    _MANIFESTS[key] = (stamp, CACHE.revision, projects)
    _MANIFESTS.move_to_end(key)
    while len(_MANIFESTS) > 16:
        _MANIFESTS.popitem(last=False)
    return sln, projects


def _select(projects, selector: str):
    if not selector and len(projects) == 1:
        return projects[0]
    matches = [p for p in projects if selector and selector.replace('\\', '/').casefold() in
               {p['name'].casefold(), str(p['path']).replace('\\', '/').casefold()}]
    if len(matches) != 1:
        raise ValueError('Select one exact HMI project: ' + ', '.join(p['name'] for p in projects))
    return matches[0]


@contextmanager
def _connection(sln: Path):
    path = sln.parent / '.TwinCATAgent' / 'hmi_source_index.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(_SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def _delete_file(db, project_file, file):
    db.execute('DELETE FROM hmi_controls WHERE project_file=? AND file=?', (project_file, file))
    db.execute('DELETE FROM hmi_files WHERE project_file=? AND file=?', (project_file, file))


def _sync_file(db, project, entry, refresh=False):
    file, project_file = entry['file'], str(project['path'])
    key = (project_file, str(file))
    old = db.execute('SELECT mtime_ns,size,epoch,parse_error FROM hmi_files WHERE project_file=? AND file=?', key).fetchone()
    stamp = _stamp(file)
    if old and not refresh and (old['mtime_ns'], old['size']) == stamp and old['epoch'] == CACHE.revision:
        return 'unchanged'
    source, stamp = _stable_text(file)
    error, controls = '', []
    if file.suffix.casefold() in _MARKUP:
        try:
            root = _xml(source)
            seen = set()
            for node in root.iter():
                if 'data-tchmi-type' not in node.attrib:
                    continue
                control_id = node.get('id', '')
                if not control_id or control_id in seen:
                    raise ValueError(f'Missing/duplicate control ID: {control_id}')
                seen.add(control_id)
                attrs = {k: v for k, v in node.attrib.items() if k.startswith('data-tchmi-')}
                # TE2000 may persist complex attributes as direct child JSON
                # scripts. Read them without migrating or rewriting the page.
                for child in node:
                    target = child.get('data-tchmi-target-attribute', '')
                    if target.startswith('data-tchmi-'):
                        if target in attrs:
                            raise ValueError(f'Ambiguous inline/script attribute: {control_id}.{target}')
                        attrs[target] = ''.join(child.itertext())
                controls.append((project_file, str(file), len(controls), control_id,
                                 node.get('data-tchmi-type'), json.dumps(attrs, ensure_ascii=False)))
        except (ET.ParseError, ValueError) as exc:
            error, controls = str(exc), []
    _delete_file(db, *key)
    db.execute('INSERT INTO hmi_files VALUES (?,?,?,?,?,?,?,?,?)',
               (*key, entry['relative'], *stamp, CACHE.revision,
                hashlib.sha256(source.encode('utf-8')).hexdigest(), source, error))
    db.executemany('INSERT INTO hmi_controls VALUES (?,?,?,?,?,?)', controls)
    return 'updated' if old else 'added'


def _sync(db, project, refresh=False):
    counts = dict(added=0, updated=0, unchanged=0, removed=0)
    issues = list(project['issues'])
    project_file = str(project['path'])
    for entry in project['files'].values():
        try:
            counts[_sync_file(db, project, entry, refresh)] += 1
            row = db.execute('SELECT parse_error FROM hmi_files WHERE project_file=? AND file=?',
                             (project_file, str(entry['file']))).fetchone()
            if row['parse_error']:
                issues.append({'file': entry['relative'], 'reason': row['parse_error']})
        except (OSError, ValueError) as exc:
            _delete_file(db, project_file, str(entry['file']))
            issues.append({'file': entry['relative'], 'reason': str(exc)})
    live_file_keys = {str(entry['file']).casefold() for entry in project['files'].values()}
    for row in db.execute('SELECT file FROM hmi_files WHERE project_file=?', (project_file,)).fetchall():
        if str(row['file']).casefold() not in live_file_keys:
            _delete_file(db, project_file, row['file'])
            counts['removed'] += 1
    return {**counts, 'issues': issues[:100], 'issue_count': len(issues),
            'issues_truncated': len(issues) > 100, 'complete': not issues}


def sync(solution: str, project: str = '', refresh: bool = False) -> dict:
    started = time.perf_counter()
    with _LOCK:
        sln, projects = _manifest(solution)
        selected = [_select(projects, project)] if project else projects
        with _connection(sln) as db:
            results = [{'project': p['name'], **_sync(db, p, refresh)} for p in selected]
    return {'status': 'indexed', 'source': 'disk_index', 'solution': str(sln),
            'projects': results, 'complete': all(r['complete'] for r in results),
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
            'live_xae': False, 'authoritative': False, 'dirty_unknown': True,
            'note': 'Only saved project references are indexed; no editor buffers or PLC runtime verified.'}


def catalog(solution: str, project: str = '', *, kind='files', file='', query='', offset=0, limit=80) -> dict:
    if kind not in {'files', 'controls'}:
        raise ValueError('kind must be files or controls')
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    with _LOCK:
        sln, projects = _manifest(solution)
        selected = _select(projects, project)
        with _connection(sln) as db:
            info = _sync(db, selected)
            if kind == 'files':
                rows = [dict(r) for r in db.execute('SELECT relative_path AS file,size,parse_error FROM hmi_files WHERE project_file=? ORDER BY relative_path', (str(selected['path']),))]
            else:
                rows = [dict(r) for r in db.execute('SELECT f.relative_path AS file,c.control_id,c.type FROM hmi_controls c JOIN hmi_files f ON c.project_file=f.project_file AND c.file=f.file WHERE c.project_file=? ORDER BY f.relative_path,c.ordinal', (str(selected['path']),))]
            rows = [r for r in rows if (not file or r['file'].casefold() == file.replace('\\', '/').casefold())
                    and (not query or query.casefold() in (r['file'] + ' ' + r.get('control_id', '') + ' ' + r.get('type', '')).casefold())]
            page = rows[offset:offset + limit]
    return {'status': 'catalog', 'project': selected['name'], 'kind': kind,
            'items': page, 'offset': offset, 'total': len(rows), 'returned_count': len(page),
            'next_offset': offset + len(page) if offset + len(page) < len(rows) else None,
            'complete': info['complete'], 'index_sync': info, 'source': 'disk_index',
            'live_xae': False, 'authoritative': False, 'dirty_unknown': True}


def _source_page(text: str, offset: int, count: int):
    raw = text.encode('utf-16-le')
    start = min(max(0, int(offset)), len(raw) // 2)
    if start and start < len(raw) // 2 and 0xDC00 <= int.from_bytes(raw[start*2:start*2+2], 'little') <= 0xDFFF:
        start -= 1
    end = min(len(raw) // 2, start + max(1, min(int(count), 1000000)))
    if end < len(raw) // 2 and 0xD800 <= int.from_bytes(raw[end*2-2:end*2], 'little') <= 0xDBFF:
        end = end + 1 if end == start + 1 else end - 1
    return raw[start*2:end*2].decode('utf-16-le'), start, end, len(raw) // 2


def read(solution: str, file: str, project: str = '', *, area='auto', control_id='',
         include_content: bool | None = None, control_offset=0, content_offset=0,
         max_controls=40, max_chars=12000, refresh=False) -> dict:
    if area not in {'auto', 'controls', 'source', 'events', 'bindings'}:
        raise ValueError('area must be auto, controls, source, events or bindings')
    started = time.perf_counter()
    with _LOCK:
        sln, projects = _manifest(solution)
        selected = _select(projects, project)
        path = (selected['path'].parent / file.replace('\\', '/')).resolve()
        entry = next((e for e in selected['files'].values() if str(e['file']).casefold() == str(path).casefold()), None)
        if not entry:
            raise ValueError('File is not a supported saved reference of the selected HMI project; use tc_hmi_source_catalog or explicit tc_hmi_read for non-indexed files')
        path = entry['file']
        mode = ('controls' if path.suffix.casefold() in _MARKUP else 'source') if area == 'auto' else area
        content = (mode == 'source') if include_content is None else bool(include_content)
        with _connection(sln) as db:
            hit = _sync_file(db, selected, entry, refresh) == 'unchanged'
            fields = 'mtime_ns,size,digest,parse_error' + (',content' if content else '')
            row = db.execute(f'SELECT {fields} FROM hmi_files WHERE project_file=? AND file=?', (str(selected['path']), str(path))).fetchone()
            if mode != 'source' and path.suffix.casefold() not in _MARKUP:
                raise ValueError('Controls/events/bindings are only available for HMI markup files; use area=source')
            if row['parse_error'] and mode != 'source':
                raise ValueError('Saved markup cannot be indexed; source remains available with area=source: ' + row['parse_error'])
            total = db.execute('SELECT COUNT(*) FROM hmi_controls WHERE project_file=? AND file=?', (str(selected['path']), str(path))).fetchone()[0]
            if control_id and mode == 'source':
                raise ValueError('Source paging is file-wide; omit control_id or select area=controls/events/bindings')
            offset = 0 if control_id else max(0, int(control_offset))
            count = max(1, min(int(max_controls), 5000))
            where, params = 'project_file=? AND file=?', [str(selected['path']), str(path)]
            if control_id:
                where += ' AND control_id=?'
                params.append(control_id)
            chosen = [] if mode == 'source' else db.execute(
                f'SELECT control_id,type,attributes FROM hmi_controls WHERE {where} ORDER BY ordinal LIMIT ? OFFSET ?',
                (*params, count, offset)).fetchall()
            if control_id and not chosen:
                raise ValueError(f'HMI control ID not found: {control_id}')
            selected_total = 0 if mode == 'source' else (1 if control_id else total)
            controls = [{'id': e['control_id'], 'type': e['type'], 'attributes': json.loads(e['attributes'])} for e in chosen]
            # A warm hit is revalidated too. Never deliver a stale DB snapshot
            # after a file changed while the query was being assembled.
            if _stamp(path) != (row['mtime_ns'], row['size']):
                raise ValueError('HMI source changed during indexed read; retry')
            result = {'status': 'read', 'project': selected['name'], 'file': entry['relative'],
                'full_path': str(path), 'project_file': str(selected['path']), 'solution': str(sln),
                'source': 'disk_index', 'read_layer': 'hmi_sqlite', 'cache_hit': hit,
                'live_xae': False, 'authoritative': False, 'dirty_unknown': True, 'source_consistent': True,
                'source_size': row['size'], 'source_mtime_ticks': row['mtime_ns'] // 100 + 621355968000000000,
                'source_hash': row['digest'], 'control_id': control_id, 'area': mode,
                'controls': controls, 'total_control_count': total, 'control_count': selected_total,
                'returned_control_count': len(controls), 'control_offset': offset,
                'controls_truncated': offset + len(controls) < selected_total,
                'next_control_offset': offset + len(controls) if offset + len(controls) < selected_total else None,
                'readonly': True, 'parse_error': row['parse_error']}
            bindings, events = [], []
            for control in controls:
                for name, value in control['attributes'].items():
                    if re.search(r'%([a-zA-Z]+)%.*?%/\1%|%\{.*?\}%', value, re.S):
                        bindings.append({'control': control['id'], 'attribute': name, 'expression': value})
                    if name == 'data-tchmi-trigger':
                        events.append({'control': control['id'], 'trigger': value})
            result.update(bindings=bindings, binding_count=len(bindings), returned_binding_count=len(bindings),
                          bindings_truncated=False, bindings_scope='selected_control_page')
            if mode == 'events':
                result['events'] = events
            result.update(content_included=content, content_chars=0, truncated=False, next_content_offset=None)
            if content:
                text, start, end, total_chars = _source_page(row['content'], content_offset, max_chars)
                result.update(content=text, content_offset=start, content_chars=end-start,
                              total_content_chars=total_chars, truncated=end < total_chars,
                              next_content_offset=end if end < total_chars else None)
            if mode in {'events', 'bindings'}:
                # Keep only requested details plus a light identity catalog.
                result['controls'] = [{'id': c['id'], 'type': c['type']} for c in controls]
            result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
            return result
