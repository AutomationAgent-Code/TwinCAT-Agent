"""Bounded parsing of live ProduceLibrarySignatures XML, not a compiler."""
import re
from xml.etree import ElementTree as ET
from .interface_knowledge import public_interface_knowledge
from .interface_types import interface_type

_ID = re.compile(r'[A-Za-z_]\w*\Z', re.ASCII)


def parse_signatures(xml):
    if not isinstance(xml, str) or len(xml) > 4_000_000:
        raise ValueError('Library signature XML exceeds budget')
    root = ET.fromstring(xml)
    name, version = root.findtext('LibraryName'), root.findtext('Version')
    if root.tag != 'Library' or not name or not version:
        raise ValueError('Missing live library identity/version')
    entries = root.findall('./TypeSignatures/TypeSignature')
    if len(entries) > 5000:
        raise ValueError('Library signature object budget exceeded')
    objects = []
    for entry in entries:
        symbol = entry.findtext('Name', '')
        if not _ID.fullmatch(symbol):
            continue
        obj = {'name': symbol, 'library': name, 'version': version,
               'source': 'live_library_signatures', 'signature_complete': False}
        knowledge = public_interface_knowledge(name, symbol)
        if entry.get('type') == 'Type':
            obj['opaque_type'] = True
            if knowledge and knowledge['kind'] == 'alias':
                obj['public_alias'] = knowledge['public_alias']
                obj['alias_source'] = knowledge['url']
                obj['interface_knowledge'] = knowledge
            elif knowledge and knowledge['kind'] == 'struct':
                # Public member types are source-level evidence, not an ABI or
                # a fabricated exact-version declaration/layout.
                obj['public_members'] = knowledge['public_members']
                obj['public_kind'] = 'struct'
                obj['interface_knowledge'] = knowledge
        # Type entries may contain only a name. Never invent their layouts.
        is_function = entry.get('type') == 'Function'
        documented_inputs = ([(n.upper(), t) for n, t in knowledge['inputs']]
                             if knowledge and knowledge['kind'] == 'memory' else None)
        actual_inputs = [(f.findtext('Name', '').upper(), interface_type(f.findtext('DataType', '')))
                         for f in entry.findall('Inputs/Input')]
        public_only = (all(c.tag in {'Name', 'Comment'} for c in entry) or
                       (actual_inputs == documented_inputs and len(entry.findall('Inputs')) == 1
                        and all(c.tag in {'Name', 'Comment', 'Inputs'} for c in entry)
                        and all(c.tag == 'Input' for c in entry.find('Inputs'))
                        and all(c.tag in {'Name', 'DataType', 'Comment'} for f in entry.findall('Inputs/Input') for c in f)))
        if is_function and documented_inputs and public_only:
            # Only enrich a name-only function from the actual referenced
            # Tc2_System library. Never override contradictory live fields.
            fields = knowledge['inputs']
            obj.update(declaration='FUNCTION ' + symbol + ' : ' + knowledge['return_type'] +
                       '\nVAR_INPUT\n' + '\n'.join(n + ' : ' + t + ';' for n, t in fields) + '\nEND_VAR',
                       return_type=knowledge['return_type'], parameter_types=[t for _, t in fields],
                       signature_complete=True, exact_version_abi_verified=False,
                       interface_knowledge=knowledge, signature_source='official_public_interface')
            objects.append(obj)
            continue
        if entry.get('type') not in {'FunctionBlock', 'Function'}:
            objects.append(obj)
            continue
        return_field = None
        if is_function:
            returns = [f for f in entry.findall('Outputs/Output')
                       if f.findtext('Name', '').casefold() == symbol.casefold()]
            if (len(returns) != 1 or not interface_type(returns[0].findtext('DataType', ''))
                    or any(c.tag not in {'Name', 'DataType', 'Comment'} for c in returns[0])):
                objects.append(obj)
                continue
            return_field = returns[0]
            header = 'FUNCTION ' + symbol + ' : ' + interface_type(return_field.findtext('DataType'))
        else:
            header = 'FUNCTION_BLOCK ' + symbol
        lines, types, seen = [header], [], set()
        complete = all(c.tag in {'Name', 'Comment', 'Inputs', 'Outputs', 'InOuts'} for c in entry)
        for group, tag, scope in [('Inputs', 'Input', 'VAR_INPUT'),
                                  ('Outputs', 'Output', 'VAR_OUTPUT'),
                                  ('InOuts', 'InOut', 'VAR_IN_OUT')]:
            containers = entry.findall(group)
            if len(containers) > 1:
                complete = False
            fields = entry.findall(group + '/' + tag)
            if containers and len(list(containers[0])) != len(fields):
                complete = False
            if not fields:
                continue
            lines.append(scope)
            for field in fields:
                if field is return_field:
                    continue  # Function return is not an output argument.
                field_name, typ = field.findtext('Name', ''), field.findtext('DataType', '')
                typ = interface_type(typ)
                if (not _ID.fullmatch(field_name) or field_name.upper() in seen
                        or not typ
                        or any(c.tag not in {'Name', 'DataType', 'Comment'} for c in field)):
                    complete = False
                    continue
                seen.add(field_name.upper())
                types.append(typ.upper())
                lines.append(field_name + ' : ' + typ + ';')
            lines.append('END_VAR')
        if complete:
            obj.update(declaration='\n'.join(lines), parameter_types=types,
                       signature_complete=True)
            if is_function:
                obj['return_type'] = interface_type(return_field.findtext('DataType'))
                if knowledge and knowledge['kind'] == 'memory':
                    actual = [(f.findtext('Name', '').upper(), interface_type(f.findtext('DataType', '')))
                              for f in entry.findall('Inputs/Input')]
                    if (actual == [(n.upper(), t) for n, t in knowledge['inputs']]
                            and obj['return_type'] == knowledge['return_type']):
                        obj['interface_knowledge'] = knowledge
            if knowledge and knowledge['kind'] == 'buffer':
                actual = {f.findtext('Name', '').upper(): f.findtext('DataType', '').upper()
                          for f in entry.findall('Inputs/Input')}
                if actual.get(knowledge['pointer']) == 'POINTER TO BYTE' and actual.get(knowledge['length']) == 'UDINT':
                    obj['interface_knowledge'] = knowledge
        objects.append(obj)
    return {'name': name, 'version': version, 'objects': objects}


def read_signatures(references):
    libraries, errors = [], []
    refs = list(references.References)
    if len(refs) > 64:
        raise ValueError('Library reference budget exceeded')
    for ref in refs:
        try:
            libraries.append(parse_signatures(str(references.ProduceLibrarySignatures(ref))))
        except Exception as exc:
            errors.append(str(exc)[:240])
    return {'status': 'read' if not errors else 'incomplete',
            'libraries': libraries, 'errors': errors, 'compiler_verified': False}
