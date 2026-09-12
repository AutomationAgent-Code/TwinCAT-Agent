"""Version-scoped HMI item scaffolding; planning is strictly read-only."""
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from .hmi_contract import Catalog, HmiContractError
from .hmi_paths import HmiPathError, normalize_relative, resolve_registered, resolve_under

TEMPLATES = {
    'view': ('View_Hmi/View.view', '.view'),
    'content': ('Content_Hmi/Partial.content', '.content'),
    'usercontrol': ('UserControl_Hmi/UserControl.usercontrol', '.usercontrol'),
    'codebehind_js': ('Code Behind/CodeBehind_Js/CodeBehindJs.js', '.js'),
    'codebehind_ts': ('Code Behind/CodeBehind_Ts/CodeBehindTs.ts', '.ts'),
    'javascript': ('General/Blank_Js/Blank_Js.js', '.js'),
    # AddItem marks the ordinary JavaScript template as JavascriptModule on
    # current TE2000.  The native path uses AddExistingItem for this variant.
    'javascript_classic': ('General/Blank_Js/Blank_Js.js', '.js'),
    'typescript': ('General/Blank_Ts/Blank_Ts.ts', '.ts'),
    'css': ('General/Blank_Css/Blank_Css.css', '.css'),
    'function_js': ('Functions/FunctionJs_Hmi/Function.js', '.js'),
    'function_js_classic': ('Functions/FunctionJs_Hmi/Function.js', '.js'),
}


def safe_relative(root, relative):
    try:
        return resolve_under(Path(root), relative)
    except HmiPathError as exc:
        raise ValueError(str(exc)) from exc


def plan_item(project_file, kind, name, folder='', parent_file='', template_root=None):
    project = Path(project_file).resolve()
    root = project.parent
    catalog = Catalog(project)
    if catalog.framework != 'native1.12-tchmi':
        raise HmiContractError('Item scaffolding currently verified only for native1.12-tchmi.')
    if kind not in TEMPLATES and kind != 'folder':
        raise ValueError('Unsupported item kind.')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('name must be an identifier without extension or path; use folder for directories.')
    relative, target = safe_relative(root, '/'.join(filter(None, (folder, name + (TEMPLATES[kind][1] if kind != 'folder' else '')))))
    if target.exists():
        raise FileExistsError('Item already exists: ' + relative)
    config_path = root / 'Properties/tchmiconfig.json'
    project_bytes, config_bytes = project.read_bytes(), config_path.read_bytes()
    project_text = project_bytes.decode('utf-8-sig')
    doc = ET.fromstring(project_text)
    registered = {str(n.get('Include')).replace('\\','/').casefold().rstrip('/') for n in doc.iter() if n.get('Include')}
    config = json.loads(config_bytes.decode('utf-8-sig'))
    files, entries = [], []
    parent = ''
    if parent_file:
        if kind not in {'codebehind_js', 'codebehind_ts', 'css'}:
            raise ValueError('parent_file is supported only for CodeBehind/CSS.')
        try:
            parent, parent_path, _ = resolve_registered(project, parent_file)
        except HmiPathError as exc:
            raise ValueError('parent_file must be one existing registered View/Content/UserControl: ' + str(exc)) from exc
        if parent_path.suffix.lower() not in {'.view','.content','.usercontrol'}:
            raise ValueError('parent_file must be an existing registered View/Content/UserControl.')
        if PurePosixPath(parent).parent != PurePosixPath(relative).parent:
            raise ValueError('Dependent item must be in the same directory as parent_file.')
    templates = Path(template_root) if template_root else Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Beckhoff/TwinCAT/Functions/TE2000-HMI-Engineering/Templates/ItemTemplates/TwinCAT HMI'
    depth = len(PurePosixPath(relative).parts) - 1
    namespace = re.sub(r'[^A-Za-z0-9_]', '_', project.stem)
    if not re.match(r'[A-Za-z_]', namespace):
        namespace = '_' + namespace
    replacements = {'$TargetIdentifier$': name, '$rootname$': name,
        '$relativedestinationdirectory$': '/'.join(['..'] * depth) or '.',
        '$default_namespace$': namespace, '$NamespaceFncTargetNameSafe$': namespace,
        '$FncTargetName$': name}

    def add(path, text, subtype='Content', dependent=''):
        rel, full = safe_relative(root, path)
        if full.exists() or rel.casefold() in registered:
            raise FileExistsError('Item collision: ' + rel)
        for token, value in replacements.items():
            text = text.replace(token, value)
        if re.search(r'\$[A-Za-z_][A-Za-z0-9_]*\$', text):
            raise ValueError('Official template has unresolved parameters.')
        files.append({'relative': rel, 'content': text})
        entries.append(f'<Content Include="{escape(rel)}"><SubType>{subtype}</SubType><Visible>true</Visible>' +
            (f'<DependentUpon>{escape(dependent)}</DependentUpon>' if dependent else '') + '</Content>')

    def register(section, entry):
        identity = entry.get('url', entry.get('name'))
        if any(x.get('url', x.get('name')) == identity for x in config.get(section, [])):
            raise ValueError('Framework registration already exists: ' + identity)
        config.setdefault(section, []).append(entry)

    def descriptor(source, schema_name):
        text = (templates/source).read_text(encoding='utf-8-sig')
        data = json.loads(text)
        schema = (config_path.parent / config['$schema']).resolve().with_name(schema_name)
        if not schema.is_file():
            raise HmiContractError('Installed descriptor Schema is missing: ' + schema_name)
        data['$schema'] = os.path.relpath(schema, target.parent).replace('\\', '/')
        return json.dumps(data, ensure_ascii=False, indent=2)

    if kind == 'folder':
        if relative.casefold() in registered:
            raise FileExistsError('Folder is already registered.')
        entries.append(f'<Folder Include="{escape(relative)}/" />')
    else:
        text = (templates / TEMPLATES[kind][0]).read_text(encoding='utf-8-sig')
        add(relative, text, dependent=PurePosixPath(parent).name if parent else '')
        if kind in {'view','content','usercontrol'}:
            catalog.validate(files[0]['content'])
            register({'view':'views','content':'content','usercontrol':'userControls'}[kind],
                     {'url': relative} if kind == 'usercontrol' else {'url': relative, 'preload': False})
        if kind == 'usercontrol':
            parameter = descriptor('UserControl_Hmi/UserControl.usercontrol.json', 'UserControlConfig.Schema.json')
            add(relative+'.json', parameter, dependent=PurePosixPath(relative).name)
        elif kind in {'function_js', 'function_js_classic'}:
            function_descriptor = descriptor('Functions/FunctionJs_Hmi/Function.function.json', 'FunctionDescription.Schema.json')
            add(str(PurePosixPath(relative).with_suffix('.function.json')), function_descriptor, dependent=PurePosixPath(relative).name)
            register('userFunctions', {'url': relative})
            register('dependencyFiles', {'name': relative, 'type': 'JavaScript', 'description': ''})
        elif kind in {'css','javascript','javascript_classic','codebehind_js'}:
            register('dependencyFiles', {'name': relative, 'type': 'Stylesheet' if kind == 'css' else 'JavaScript', 'description': ''})
        elif kind in {'typescript','codebehind_ts'}:
            # Project-generated JS location must be established, never guessed.
            raise HmiContractError('TypeScript creation is not enabled until this project output-path contract is verified; use JavaScript or the XAE template wizard.')
    closing = project_text.rfind('</Project>')
    if closing < 0:
        raise ValueError('Missing project closing element.')
    updated = project_text[:closing] + '<ItemGroup>' + ''.join(entries) + '</ItemGroup>\n' + project_text[closing:]
    return {'kind':kind, 'relative':relative, 'files':files, 'project_file':str(project),
        'project_sha256':hashlib.sha256(project_bytes).hexdigest(),
        'config_sha256':hashlib.sha256(config_bytes).hexdigest(),
        'project_content':updated, 'config_content':json.dumps(config, ensure_ascii=False, indent=2),
        'framework':catalog.framework, 'contract_stamps':list(catalog.stamps.values()), 'template_source':str(templates),
        'reload_required':True, 'browser_verified':False,
        'note':'CodeBehind template uses global onInitialized, not page lifecycle; parent_file is project nesting only.'}


def create_item(kind, name, project='', folder='', parent_file='', apply=False):
    from . import _ps_bridge as ps
    info = ps.com_hmi_project_info(project)
    catalog = Catalog(Path(info['project_file']))
    if catalog.framework != 'native1.12-tchmi':
        raise HmiContractError('Native item creation is verified only for native1.12-tchmi.')
    result = ps.ps_com('hmi-item-apply', project=info['project_file'], kind=kind,
                     name=name, folder=folder, parent_file=parent_file, apply=apply, timeout=60)
    if result.get('written') is True:
        try:
            from .hmi_item_evidence import inspect_created_item
            evidence = inspect_created_item(info['project_file'], result['relative'], kind)
            result['saved_state_evidence'] = evidence
            if not evidence['saved_state_verified']:
                result.update(status='incomplete', verified=False, retry_safe=False,
                              error='Native creation synchronization incomplete: ' + ', '.join(evidence['failed_checks']))
        except Exception as exc:
            result.update(status='incomplete', verified=False, retry_safe=False,
                          saved_state_evidence={'saved_state_verified': False, 'error': str(exc)},
                          error='Cannot verify native creation synchronization: ' + str(exc))
    if result.get('written') is True and kind in {'view', 'content', 'usercontrol'}:
        try:
            saved = Path(info['project_file']).parent / result['relative']
            catalog.validate(saved.read_text(encoding='utf-8-sig'))
            result['schema_verified'] = True
        except Exception as exc:
            result.update(status='incomplete', verified=False, schema_verified=False,
                          error=str(exc), retry_safe=False)
    return result
