"""Offline, installed-version HMI write contracts. No network/schema guessing."""
from __future__ import annotations
import hashlib
import html
import difflib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET


class HmiContractError(ValueError):
    def __init__(self, message, findings=None):
        super().__init__(message)
        self.details = {'error_type': 'hmi_write_contract', 'written': False,
                        'findings': (findings or [])[:40],
                        'finding_count': len(findings or []), 'findings_truncated': len(findings or []) > 40}


def _xml(text):
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', text, re.I):
        raise HmiContractError('DTD/entity declarations are forbidden')
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # TE2000 stores application/json script elements as HTML raw text.
        # A JSON comparison operator "<" is valid there but not in XML text.
        # Escape only independently valid JSON, and only in the parser copy.
        def raw_json(match):
            raw = match[2]
            try:
                json.loads(raw)
            except ValueError:
                return match[0]
            return match[1] + html.escape(raw, quote=False) + match[3]
        prepared = re.sub(r'(<script\b[^>]*\btype=[\"\']application/json[\"\'][^>]*>)([\s\S]*?)(</script\s*>)',
                          raw_json, text, flags=re.I)
        return ET.fromstring(prepared)


def digest(text):
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def _trigger_map(markup):
    """Return saved Trigger payloads without interpreting project instructions."""
    return {
        node.get('id', ''): node.get('data-tchmi-trigger', '')
        for node in _xml(markup).iter()
        if node.get('id') and node.get('data-tchmi-trigger') is not None
    }


def _assert_dedicated_event_write(command, clean, preview, event_placement):
    """Force event mutations through hmi_events' installed-contract checks."""
    if event_placement:
        if event_placement not in {'native', 'custom'}:
            raise HmiContractError('Invalid internal event placement proof')
        return
    candidate = _trigger_map(preview['markup'])
    if not candidate:
        return
    source = {}
    source_file = preview.get('source_file')
    if source_file:
        path = Path(source_file).resolve()
        if path.is_file():
            source = _trigger_map(path.read_text(encoding='utf-8-sig'))
    if candidate != source:
        raise HmiContractError(
            'HMI Trigger mutation is only allowed through tc_hmi_control_events; '
            'read the installed event contract first and use placement=native by default',
            [{'control': control, 'attribute': 'data-tchmi-trigger',
              'message': 'Generic HMI write attempted to add or change an event Trigger'}
             for control in sorted(set(candidate) | set(source)) if candidate.get(control) != source.get(control)]
        )


def _semantic_page_markup(name: str, kind: str, controls: list[dict]) -> str:
    """Build an in-memory validation candidate for native page creation.

    This is not a source template and is never written to disk.  The apply
    path creates the page through TE2000 AddView/AddContent and adds the same
    controls through AddControl; the candidate only gives the installed
    Catalog a bounded object to validate before COM is allowed to mutate.
    """
    base = Path(str(name)).stem
    root = ET.Element('div', {
        'id': base,
        'data-tchmi-type': 'TcHmi.Controls.System.TcHmiView' if kind == 'view'
        else 'TcHmi.Controls.System.TcHmiContent',
        'data-tchmi-top': '0', 'data-tchmi-left': '0',
        'data-tchmi-width-mode': 'Content', 'data-tchmi-min-width': '100',
        'data-tchmi-min-width-unit': '%', 'data-tchmi-height-mode': 'Content',
        'data-tchmi-min-height': '100', 'data-tchmi-min-height-unit': '%',
    })
    nodes = {base: root}
    for item in controls:
        identifier = str(item.get('id') or '')
        control_type = str(item.get('type') or '')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', identifier):
            raise HmiContractError(f'Invalid HMI control id: {identifier}')
        if identifier in nodes:
            raise HmiContractError(f'Duplicate HMI control id: {identifier}')
        if not control_type.startswith('TcHmi.Controls.'):
            raise HmiContractError(f'Invalid HMI control type: {control_type}')
        node = ET.Element('div', {'id': identifier, 'data-tchmi-type': control_type})
        for key, value in (item.get('attributes') or {}).items():
            if not str(key).startswith('data-tchmi-') or key in {'data-tchmi-type', 'data-tchmi-trigger'}:
                raise HmiContractError(f'Only non-event data-tchmi-* attributes are allowed: {key}')
            node.set(str(key), str(value))
        parent = str(item.get('parent_id') or base)
        if parent not in nodes:
            raise HmiContractError(f"Parent control '{parent}' was not found")
        nodes[parent].append(node)
        nodes[identifier] = node
    return ET.tostring(root, encoding='unicode')


class Catalog:
    def __init__(self, project_file):
        self.project = Path(project_file).resolve()
        self.stamps = {}
        self.controls = {}
        self.control_paths = {}
        self.definitions = {}
        self.definition_candidates = {}
        self.conflicts = set()
        self.stores = {}
        root = _xml(self.text(self.project))
        fields = {n.tag.split('}')[-1]: n.text for n in root.iter()}
        self.framework = fields.get('TargetFramework')
        if not self.framework:
            m = re.fullmatch(r'([^,]+),Version=v(\d+\.\d+)(?:\.\d+)?,Profile=(.+)', fields.get('TargetFrameworkMoniker', ''))
            if not m:
                raise HmiContractError('Cannot establish exact HMI Framework target')
            self.framework = m[1] + m[2] + '-' + m[3]
        packages = _xml(self.text(self.project.parent / 'packages.config'))
        package_root = self.project.parent.parent / 'Packages'
        config_file = self.project.parent / 'Properties/tchmiconfig.json'
        if config_file.is_file():
            schema_ref = json.loads(self.text(config_file)).get('$schema', '')
            if schema_ref and not re.match(r'^[a-z]+://', schema_ref, re.I):
                schema_path = (config_file.parent / schema_ref.replace('\\', '/')).resolve()
                if schema_path.name.lower() == 'tchmiconfig.schema.json' and schema_path.is_file():
                    runtime = schema_path.parent.parent
                    package_folder = runtime.parent.parent
                    declared = {p.attrib['id'] + '.' + p.attrib['version'] for p in packages.findall('package')}
                    if (runtime.name != self.framework or runtime.parent.name != 'runtimes' or
                            package_folder.name not in declared):
                        raise HmiContractError('Recorded configuration schema does not match the declared package/framework')
                    # XAE supports projects outside the solution directory. Use its
                    # exact saved reference, not a guessed sibling Packages folder.
                    package_root = package_folder.parent
                    self.text(schema_path)
        declared_runtimes = []
        for package in packages.findall('package'):
            folder = (package_root / (package.attrib['id'] + '.' + package.attrib['version'])).resolve()
            if not folder.is_relative_to(package_root.resolve()):
                raise HmiContractError('Package reference escapes Packages root')
            runtime = folder / 'runtimes' / self.framework
            if not runtime.is_dir():
                # Server has a different native runtime; use its exact version.
                for name, uri in [('tchmi.general.Schema.json', 'tchmi:general'), ('TcHmiSrv.Schema.json', 'tchmi:server')]:
                    candidates = sorted((folder / 'runtimes').glob('win-x64/**/' + name))
                    if not candidates:
                        candidates = sorted((folder / 'runtimes').glob('win-x86/**/' + name))
                    if len(candidates) == 1:
                        self.stores[uri] = json.loads(self.text(candidates[0]))
                continue
            declared_runtimes.append(runtime)
            manifest_path = runtime / 'Manifest.json'
            if manifest_path.is_file():
                manifest = json.loads(self.text(manifest_path))
                self._load_data_types(manifest_path, manifest, runtime)
            for path in sorted(runtime.rglob('Description.json')):
                data = json.loads(self.text(path))
                key = str(data.get('namespace', '')) + '.' + str(data.get('name', ''))
                if data.get('namespace') and data.get('name'):
                    if key in self.controls:
                        raise HmiContractError('Ambiguous installed control type: ' + key)
                    self.controls[key] = data
                    self.control_paths[key] = path
                self._load_data_types(path, data, runtime)
        # Official packages retain deprecated short names as aliases to their
        # namespaced types. Compare resolved constraints, not raw alias text.
        def constraints(value, visiting=()):
            if isinstance(value, list):
                return [constraints(x, visiting) for x in value]
            if not isinstance(value, dict):
                return value
            meaningful = {k: v for k, v in value.items() if k not in {'title', 'description', 'default', '$comment'}}
            ref = meaningful.get('$ref', '')
            prefix = 'tchmi:framework#/definitions/'
            if set(meaningful) == {'$ref'} and ref.startswith(prefix):
                name = ref[len(prefix):]
                if name in self.definitions and name not in visiting:
                    return constraints(self.definitions[name], visiting + (name,))
            result = {}
            for k, v in meaningful.items():
                if k in {'properties', 'patternProperties', 'definitions'}:
                    result[k] = {name: constraints(schema, visiting) for name, schema in v.items()}
                elif k in {'enum', 'required', 'type'} and isinstance(v, list):
                    result[k] = sorted(v, key=lambda x: json.dumps(x, sort_keys=True))
                else:
                    result[k] = constraints(v, visiting)
            return result
        self.conflicts = {name for name, values in self.definition_candidates.items()
                          if any(constraints(v) != constraints(values[0]) for v in values[1:])}
        for name in self.conflicts:
            self.definitions.pop(name, None)  # ambiguous schemas fail if used
        self.stores['tchmi:framework'] = {'definitions': self.definitions}
        # Some shipped JSON schemas describe a single comparison although the
        # SAME package's public runtime API requires Expression[]. Do not
        # corrupt working multi-comparison events to match that stale schema.
        for runtime in declared_runtimes:
            declarations = [runtime / 'dist/API/Trigger.d.ts', runtime / 'dist/TcHmiCore/_Types.d.ts',
                            runtime / 'TcHmiFramework.d.ts']
            existing = [p for p in declarations if p.is_file()]
            if not existing or 'Trigger' not in self.definitions:
                continue
            api = '\n'.join(self.text(p) for p in existing)
            if not (re.search(r'interface ConditionIf\s*\{\s*if:\s*Expression\[\]', api) and
                    re.search(r'interface ConditionElseIf\s*\{\s*elseif:\s*Expression\[\]', api)):
                continue
            for action in self.definitions['Trigger'].get('definitions', {}).get('action', {}).get('anyOf', []):
                if action.get('properties', {}).get('objectType', {}).get('enum') != ['Condition']:
                    continue
                for part in action['properties']['parts']['items']['anyOf']:
                    for key in ('if', 'elseif'):
                        old = part.get('properties', {}).get(key)
                        if old == {'$ref': '#/definitions/Trigger/definitions/expression'}:
                            part['properties'][key] = {'type': 'array', 'items': old}
        project_schema = self.project.parent / 'Properties/tchmi.project.Schema.json'
        registered = {n.get('Include', '').replace('\\', '/') for n in root.iter()
                      if n.tag.rsplit('}', 1)[-1] == 'Content'}
        if 'Properties/tchmi.project.Schema.json' in registered and project_schema.is_file():
            self.stores['tchmi:project'] = json.loads(self.text(project_schema))
        if not self.controls:
            raise HmiContractError('No installed controls found for the exact project Framework')

    def _load_data_types(self, document, data, runtime):
        # Modern packages declare shared primitive types in Manifest.json, not
        # just individual control descriptions. Resolve only declared files.
        for item in data.get('dataTypes', []):
            schema_path = (document.parent / item['schema']).resolve()
            if not schema_path.is_relative_to(runtime.resolve()):
                raise HmiContractError('Schema reference escapes installed runtime')
            schema = json.loads(self.text(schema_path))
            for name, value in schema.get('definitions', {}).items():
                self.definition_candidates.setdefault(name, []).append(value)
                if name in self.definitions and self.definitions[name] != value:
                    self.conflicts.add(name)
                self.definitions[name] = value

    def text(self, path):
        path = Path(path).resolve()
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise HmiContractError('Contract file changed during read; retry')
        self.stamps[str(path)] = {'path': str(path), 'size': after.st_size,
            'ticks': after.st_mtime_ns // 100 + 621355968000000000}
        return raw.decode('utf-8-sig')

    def attributes(self, name, visiting=()):
        if name in visiting or name not in self.controls:
            raise HmiContractError('Unknown/cyclic installed control type: ' + name)
        data = self.controls[name]
        attrs = self.attributes(data['base'], visiting + (name,)) if data.get('base') else {}
        attrs.update({a['name']: a for a in data.get('attributes', [])})
        attrs.update({a['name']: a for a in data.get('creator', {}).get('attributes', [])})
        return attrs

    def node_attributes(self, node):
        attrs = self.attributes(node.get('data-tchmi-type'))
        if node.get('data-tchmi-type') != 'TcHmi.Controls.System.TcHmiUserControlHost':
            return attrs
        target = node.get('data-tchmi-target-user-control', '')
        if not target or '%' in target or re.match(r'^[a-z]+:', target, re.I):
            raise HmiContractError('UserControlHost needs a literal local UserControl path')
        path = (self.project.parent / target.replace('\\', '/')).resolve()
        if not path.is_relative_to(self.project.parent) or path.suffix.lower() != '.usercontrol':
            raise HmiContractError('UserControl target escapes the project or has an invalid extension')
        registered = {n.get('Include', '').replace('\\', '/') for n in _xml(self.text(self.project)).iter()
                      if n.tag.split('}')[-1] == 'Content'}
        if path.relative_to(self.project.parent).as_posix() not in registered:
            raise HmiContractError('UserControl target is not registered in this project')
        self.text(path)
        parameters = json.loads(self.text(path.with_suffix('.usercontrol.json'))).get('parameters', [])
        names = set()
        for parameter in parameters:
            name = parameter.get('name', '')
            if not name.startswith('data-tchmi-') or name in names or name in attrs:
                raise HmiContractError('Invalid/duplicate UserControl parameter name: ' + name)
            if not isinstance(parameter.get('type'), str) or not parameter['type'].startswith('tchmi:'):
                raise HmiContractError('UserControl parameter needs a local tchmi schema reference')
            names.add(name)
            attrs[name] = parameter
        return attrs

    def validator(self, ref):
        from jsonschema import Draft4Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT4
        def unavailable(uri):
            raise HmiContractError('Schema is not available locally: ' + uri)
        registry = Registry(retrieve=unavailable)
        for uri, schema in self.stores.items():
            registry = registry.with_resource(uri, Resource.from_contents(schema, default_specification=DRAFT4))
        return Draft4Validator({'$ref': ref}, registry=registry)

    def validate(self, markup, control_id='', *, control_ids=None):
        root = _xml(markup)
        nodes = [n for n in root.iter() if n.get('data-tchmi-type')]
        ids = [n.get('id', '') for n in nodes]
        if not nodes or any(not re.fullmatch(r'[A-Za-z0-9_]+', i) for i in ids) or len(set(ids)) != len(ids):
            raise HmiContractError('Missing/invalid/duplicate HMI control ID')
        wanted = set(control_ids) if control_ids is not None else ({control_id} if control_id else None)
        selected = [n for n in nodes if wanted is None or n.get('id') in wanted]
        if wanted is not None and not wanted.issubset(ids):
            raise HmiContractError('Target control is absent from candidate markup')
        if not selected and control_ids is None:
            raise HmiContractError('Target control is absent from candidate markup')
        findings, bindings = [], []
        # Decorative controls cannot host arbitrary child controls. This is
        # declared by installed descriptions, not inferred from HTML nesting.
        for node in nodes:
            definition = self.controls.get(node.get('data-tchmi-type'), {})
            if definition.get('properties', {}).get('containerControl') is False and any(
                child is not node and child.get('data-tchmi-type') for child in node.iter()
            ):
                findings.append({'control':node.get('id'),'type':node.get('data-tchmi-type'),
                    'message':'Installed control has containerControl=false; use a real container for nested controls'})
        for node in selected:
            name, kind = node.get('id'), node.get('data-tchmi-type')
            try:
                attrs = self.node_attributes(node)
            except HmiContractError as exc:
                findings.append({'control': name, 'type': kind, 'message': str(exc)})
                continue
            values = {k: (v, False) for k, v in node.attrib.items() if k.startswith('data-tchmi-') or k == 'id'}
            for child in node:
                key = child.get('data-tchmi-target-attribute')
                if key:
                    if key in values:
                        findings.append({'control': name, 'attribute': key, 'message': 'Duplicate inline/script attribute'})
                    values[key] = (''.join(child.itertext()), True)
            for key, (raw, script) in values.items():
                try:
                    from .hmi_symbols import check_symbol_value
                    check_symbol_value(raw)
                    if key in {'data-tchmi-creator-viewport-width', 'data-tchmi-creator-viewport-height'}:
                        if not re.fullmatch(r'\d+(\.\d+)?', raw):
                            raise ValueError('Designer viewport must be a nonnegative number')
                        continue
                    if key not in attrs:
                        raise ValueError('Attribute is not declared by this control or its base classes')
                    attr = attrs[key]
                    compile_only_target = (kind == 'TcHmi.Controls.System.TcHmiUserControlHost'
                                           and key == 'data-tchmi-target-user-control'
                                           and attr.get('requiredOnCompile') is True)
                    if attr.get('readOnly') and key not in {'id', 'data-tchmi-type'} and not compile_only_target:
                        raise ValueError('Read-only attribute cannot be assigned')
                    if re.fullmatch(r'%(s|i|ctrl|pp|l|tr|f)%[\s\S]+%/\1%', raw):
                        if attr.get('bindable') is False:
                            raise ValueError('Attribute is not bindable')
                        bindings.append({'control': name, 'attribute': key, 'expression': raw})
                        continue
                    if re.match(r'^%(?:\{|[a-z]+%)', raw):
                        raise ValueError('Unrecognized/malformed SymbolExpression')
                    candidates = []
                    try:
                        candidates.append(json.loads(raw))
                    except (ValueError, TypeError):
                        if script:
                            raise ValueError('Script-backed attribute must contain valid JSON')
                    if not script:
                        candidates.append(raw)
                        # Existing TE2000 designer files use True/False as well
                        # as lowercase JSON booleans. The schema still decides
                        # whether a boolean is admissible for this attribute.
                        if raw in {'True', 'False'}:
                            candidates.append(raw == 'True')
                    validator = self.validator(attr['type'])
                    errors = [list(validator.iter_errors(v)) for v in candidates]
                    if all(errors):
                        raise ValueError('Value does not match ' + attr['type'] + ': ' + errors[0][0].message[:240])
                except Exception as exc:
                    findings.append({'control': name, 'type': kind, 'attribute': key, 'message': str(exc)[:400]})
        if findings:
            raise HmiContractError(f'HMI write blocked: {len(findings)} control/schema violations', findings)
        return {'validated': True, 'scope': 'selected_controls' if control_ids is not None else ('selected_control' if control_id else 'whole_markup'),
                'framework': self.framework, 'checked_controls': len(selected),
                'runtime_bindings_verified': False, 'deferred_bindings': bindings[:30],
                'deferred_binding_count': len(bindings), 'browser_verified': False}


def guarded_call(command, args, call):
    """Preview -> validate exact candidate -> apply with fail-closed PS proof."""
    clean = {k: v for k, v in args.items() if k != 'hmi_write_gate'}
    candidate_source_hash = clean.pop('_candidate_source_hash', '')
    event_placement = str(clean.pop('_event_placement', '') or '')
    if command == 'hmi-controls-batch':
        from .hmi_batch import validate_operations
        validate_operations(clean.get('operations'))
    preview = call(command, **{**clean, 'apply': False})
    if not isinstance(preview, dict) or preview.get('status') != 'preview' or not preview.get('project_file'):
        raise HmiContractError('HMI write preview lacks its exact project contract; update the COM helper')
    if candidate_source_hash and preview.get('source_hash') != candidate_source_hash:
        raise HmiContractError('Page changed after draft repair; candidate was not written')
    catalog = Catalog(preview['project_file'])
    selected = str(clean.get('control_id') or '') if command == 'hmi-control-edit' else ''
    try:
        if command == 'hmi-create-view':
            # Native preview normally has no source markup.  If a stale/legacy
            # adapter supplies one, still apply the dedicated event gate
            # before replacing it with the semantic validation candidate.
            if preview.get('markup'):
                _assert_dedicated_event_write(command, clean, preview, event_placement)
            candidate = _semantic_page_markup(clean.get('name', ''), clean.get('kind', 'view'),
                                              list(clean.get('controls') or []))
            preview['markup'] = candidate
            selected_ids = [str(item.get('id')) for item in (clean.get('controls') or [])]
            validation = catalog.validate(candidate, control_ids=selected_ids or None)
        elif command == 'hmi-controls-batch':
            _assert_dedicated_event_write(command, clean, preview, event_placement)
            selected_ids = [op['control_id'] for op in clean['operations'] if op['action'] != 'remove']
            validation = catalog.validate(preview['markup'], control_ids=selected_ids)
        else:
            _assert_dedicated_event_write(command, clean, preview, event_placement)
        if command == 'hmi-control-edit' and clean.get('action') == 'remove':
            _xml(preview['markup'])
            validation = {'validated': True, 'scope': 'removal_only', 'browser_verified': False}
        elif command not in {'hmi-create-view', 'hmi-controls-batch'}:
            validation = catalog.validate(preview['markup'], selected)
    except HmiContractError as exc:
        if command == 'hmi-write-markup':
            from .hmi_candidates import remember
            candidate_id = remember({**preview, 'file': clean.get('file', '')})
            if candidate_id:
                exc.details.update(candidate_id=candidate_id,
                    next_action='Use tc_hmi_write_markup with candidate_id and exact replacements; do not regenerate the whole page')
        raise
    preview['write_contract'] = validation
    if not clean.get('apply'):
        return preview
    proof = {'project_file': str(catalog.project), 'candidate_hash': digest(preview['markup']),
             'files': list(catalog.stamps.values())}
    if preview.get('source_file'):
        proof.update(source_file=preview['source_file'], source_hash=preview['source_hash'])
    result = call(command, **{**clean, 'hmi_write_gate': proof})
    if isinstance(result, dict):
        result['write_contract'] = validation
        result['browser_verification_required'] = True
    return result


def control_schema(project='', control_type='', attribute=''):
    from ._ps_bridge import ps_com
    info = ps_com('hmi-project-info', project=project)
    catalog = Catalog(info['project_file'])
    if not control_type:
        return {'framework': catalog.framework, 'control_types': sorted(catalog.controls),
                'readonly': True, 'source': 'installed_package_descriptions'}
    if control_type not in catalog.controls:
        exc = HmiContractError('Unknown installed control type: ' + control_type)
        exc.details.update(code='unknown_control_type', retry_safe=False,
            candidates=difflib.get_close_matches(control_type, sorted(catalog.controls), n=5, cutoff=0.4),
            next_action='Select an installed candidate or list control types; do not retry the same unknown name.')
        raise exc
    attrs = catalog.attributes(control_type)
    if attribute and attribute not in attrs:
        exc = HmiContractError('Attribute does not exist: ' + attribute)
        exc.details.update(code='unknown_attribute', retry_safe=False, control_type=control_type,
            attribute_names=sorted(attrs),
            candidates=difflib.get_close_matches(attribute, sorted(attrs), n=5, cutoff=0.4),
            next_action='Use an exact attribute_names entry; candidates are suggestions, never automatic replacements.')
        raise exc
    selected = {attribute: attrs[attribute]} if attribute else attrs
    result = {'framework': catalog.framework, 'control_type': control_type,
              'container_control': catalog.controls[control_type].get('properties', {}).get('containerControl'),
              'attributes': [{k: a[k] for k in ('name', 'type', 'readOnly', 'bindable', 'defaultValue', 'description') if k in a}
                             for a in selected.values()], 'readonly': True}
    if attribute:
        # Include bounded related definitions so the model can construct values
        # (Color -> SolidColor, for example), without guessing schema references.
        pending = [attrs[attribute]['type']]
        schemas = {}
        while pending and len(schemas) < 12:
            ref = pending.pop(0)
            if ref in schemas:
                continue
            uri, _, fragment = ref.partition('#')
            value = catalog.stores.get(uri)
            try:
                for token in fragment.lstrip('/').split('/'):
                    value = value[token.replace('~1', '/').replace('~0', '~')]
            except (KeyError, TypeError):
                continue
            raw = json.dumps(value)
            if len(raw) > 5000:
                schemas[ref] = {'details_omitted': True, 'reason': 'Definition too large; validation still uses full schema'}
                continue
            schemas[ref] = value
            for child in re.findall(r'"\$ref"\s*:\s*"([^"]+)"', raw):
                pending.append(uri + child if child.startswith('#') else child)
        result['schemas'] = schemas
        result['schemas_truncated'] = bool(pending)
    return result


def validate_project(project_file):
    """Saved registered markup validation; no browser/ADS claims."""
    catalog = Catalog(project_file)
    root = _xml(catalog.text(catalog.project))
    findings, checked, total_errors = [], 0, 0
    seen = set()
    for node in root.iter():
        include = node.get('Include', '').replace('\\', '/')
        if node.tag.split('}')[-1] != 'Content' or Path(include).suffix.lower() not in {'.view', '.content', '.usercontrol'}:
            continue
        path = (catalog.project.parent / include).resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            if not path.is_relative_to(catalog.project.parent):
                raise HmiContractError('Linked markup escapes HMI project root')
            catalog.validate(catalog.text(path))
        except Exception as exc:
            details = getattr(exc, 'details', {}).get('findings') or [{'message': str(exc)}]
            total_errors += getattr(exc, 'details', {}).get('finding_count') or len(details)
            findings.extend({**d, 'file': include, 'severity': 'error', 'rule': 'hmi-control-schema'} for d in details)
        checked += 1
    return {'schema_checked_markup_files': checked, 'schema_error_count': total_errors,
            'schema_findings': findings[:100], 'schema_findings_truncated': total_errors > min(len(findings), 100),
            'schema_valid': not findings, 'browser_verified': False, 'runtime_bindings_verified': False}
