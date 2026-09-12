"""Validate symbol syntax even in JSON properties or damaged binding attempts."""
import json
import re

TOKENS = re.compile(r'%(/?)([A-Za-z]+)%')
SERVER_EXPRESSIONS = re.compile(r'%s%(.*?)%/s%', re.I | re.S)
TAGS = {'s', 'i', 'ctrl', 'pp', 'l', 'tr', 'f'}


def _check_server_paths(raw):
    """Reject ADS mapping paths accidentally pasted as HMI server symbols.

    ``PLC1::GVL::Value`` is the value stored in TcHmiSrv's ``MAPPING``
    field.  A page must bind to the corresponding server symbol key, for
    example ``ADS.PLC1.GVL.Value``.  A ``::`` after a dotted server root is
    still allowed because TwinCAT uses that form for some member paths.
    """
    for match in SERVER_EXPRESSIONS.finditer(raw):
        path = match.group(1).strip()
        mapping_separator = path.find('::')
        server_separator = path.find('.')
        if mapping_separator >= 0 and (
                server_separator < 0 or mapping_separator < server_separator):
            raise ValueError(
                'Raw ADS MAPPING path cannot be used as a page SymbolExpression: '
                'use the registered TcHmiSrv symbol key, normally '
                '%s%ADS.<Runtime>.<PLC symbol>%/s%, not '
                '%s%<Runtime>::<PLC symbol>%/s%'
            )


def check_symbol_value(raw, *, _event_name=False, _action_context=False):
    if not isinstance(raw, str):
        if isinstance(raw, dict):
            for key, value in raw.items():
                if raw.get('objectType') == 'JavaScript' and key == 'sourceLines':
                    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
                        raise ValueError('JavaScript sourceLines must be a string array')
                    # Executable JavaScript is not a value SymbolExpression.
                    # Action structure/schema checks remain mandatory.
                    continue
                if raw.get('objectType') == 'FunctionExpression' and key == 'functionExpression' and isinstance(value, str):
                    check_symbol_value('%f%' + value + '%/f%', _action_context=True)
                    continue
                check_symbol_value(value, _event_name=(key == 'event'),
                                   _action_context=(key == 'symbolExpression' and raw.get('objectType') in {'Symbol', 'WriteToSymbol'}))
        elif isinstance(raw, list):
            for value in raw: check_symbol_value(value)
        return
    # Native event names are an owner-context expression plus an event suffix,
    # not a value binding. Only this exact form in an `event` field is allowed.
    if _event_name and re.fullmatch(r'%ctx%owner::Id\|EventRegistrationMode=Resolve%/ctx%\.on[A-Za-z0-9_]+', raw):
        return
    if not TOKENS.search(raw):
        return
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, (dict, list)):
        check_symbol_value(decoded)
        return
    _check_server_paths(raw)
    tokens = list(TOKENS.finditer(raw))
    if tokens[0].start() != 0 or tokens[-1].end() != len(raw):
        raise ValueError('Malformed SymbolExpression: remove prefixes/suffixes such as =; use %s%...%/s% or a %f% expression')
    stack = []
    for token in tokens:
        closing, tag = token.groups()
        if tag == 'ctx' and _action_context:
            if not closing:
                end = raw.find('%/ctx%', token.end())
                if end < 0 or not re.fullmatch(r'owner(?:::[A-Za-z_][A-Za-z0-9_:]*)?', raw[token.end():end]):
                    raise ValueError('Unsupported action owner context')
        elif tag not in TAGS:
            raise ValueError('Unknown SymbolExpression tag: ' + tag + '; PLC types are not closing tags')
        if closing:
            if not stack or stack.pop() != tag:
                raise ValueError('Mismatched SymbolExpression tags')
            if not stack and token.end() != len(raw):
                raise ValueError('Multiple root SymbolExpressions require a %f% expression')
        else:
            stack.append(tag)
    if stack:
        raise ValueError('Unclosed SymbolExpression tag')


def saved_symbol_findings(project_file):
    from pathlib import Path
    from .hmi_contract import _xml
    project = Path(project_file).resolve()
    findings, seen = [], set()
    for item in _xml(project.read_text(encoding='utf-8-sig')).iter():
        include = item.get('Include', '').replace('\\', '/')
        if item.tag.rsplit('}',1)[-1] != 'Content' or Path(include).suffix.lower() not in {'.view','.content','.usercontrol'}:
            continue
        path = (project.parent / include).resolve()
        if path in seen: continue
        seen.add(path)
        if not path.is_relative_to(project.parent):
            raise ValueError('HMI registered markup escapes project root')
        for node in _xml(path.read_text(encoding='utf-8-sig')).iter():
            values = dict(node.attrib)
            if node.get('data-tchmi-target-attribute'):
                values[node.get('data-tchmi-target-attribute')] = ''.join(node.itertext())
            for key, value in values.items():
                if not key.startswith('data-tchmi-'): continue
                try: check_symbol_value(value)
                except ValueError as exc:
                    code = ('binding-mapping-path-invalid'
                            if 'Raw ADS MAPPING path' in str(exc) else 'binding-malformed')
                    findings.append({'severity':'error','code':code,'file':include,
                        'control':node.get('id',''),'attribute':key,'message':str(exc)})
    return findings
