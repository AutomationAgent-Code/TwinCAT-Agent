"""Offline candidate analysis with an explicit, bounded dependency resolver.

No engineering mutations. Unsupported semantics produce incomplete evidence,
not an approval. Actual TwinCAT compilation remains a separate evidence field.
"""
import re
from .lint import _st_code
from .st_parser import Node, parse_implementation
from .interface_types import declared_return_type, string_spec
from .integer_literals import integer_literal, fits_integer, WIDTHS
from .type_contract import alias_target, array_type, subrange, enum_definition
from .date_literals import ALIASES as DATE_ALIASES, date_literal_error

INTS = {'SINT', 'USINT', 'INT', 'UINT', 'DINT', 'UDINT', 'LINT', 'ULINT'}
REALS = {'REAL', 'LREAL'}
BITS = {'BYTE', 'WORD', 'DWORD', 'LWORD'}
SCALARS = INTS | REALS | BITS | {'BOOL', 'STRING', 'WSTRING', 'TIME', 'LTIME', 'DATE', 'LDATE', 'DT', 'LDT', 'TOD', 'LTOD', 'PVOID', '__XWORD', '__UXINT'}


def declarations(source):
    """Read common split-editor declarations without inventing missing types."""
    from .conditional_compilation import preprocess_editor
    selected=preprocess_editor(source,'declaration')
    if selected['status']!='processed':
        return {}, [selected['reason']]
    source=selected['source']
    code = _st_code(source)
    symbols, issues = {}, []
    if re.search(r'\{\s*(?:IF|ELSIF|ELSE|END_IF)\b', code, re.I):
        issues.append('Conditional declaration pragmas are not semantically verified.')
    blocks = list(re.finditer(r'\b(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_GLOBAL|_TEMP|_STAT|_INST|_EXTERNAL)?)\b(.*?)\bEND_VAR\b', code, re.I | re.S))
    fields = re.search(r'\b(?:STRUCT|UNION)\b(.*?)\bEND_(?:STRUCT|UNION)\b', code, re.I | re.S)
    sections = [(m[1].upper(), m[2]) for m in blocks]
    if fields:
        sections.append(('FIELD', fields[1]))
    for scope, body in sections:
        qualifiers = re.match(r'^\s*((?:(?:CONSTANT|RETAIN|NON_RETAIN|PERSISTENT)\s+)*)', body, re.I)[1]
        constant = bool(re.search(r'\bCONSTANT\b', qualifiers, re.I))
        body = re.sub(r'^\s*(?:(?:CONSTANT|RETAIN|NON_RETAIN|PERSISTENT)\s+)*', '', body, flags=re.I)
        for statement in body.split(';'):
            if not statement.strip():
                continue
            match = re.fullmatch(r'\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:AT\s+%[^:]+)?\s*:\s*(.+?)\s*', statement, re.S | re.I)
            if not match:
                issues.append('Unsupported or malformed declaration: ' + statement.strip()[:120])
                continue
            specification = match[2].split(':=', 1)[0].strip().upper()
            specification = re.sub(r'\b(?:DATE_AND_TIME|TIME_OF_DAY|LDATE_AND_TIME|LTIME_OF_DAY)\b', lambda m: DATE_ALIASES[m[0]], specification)
            if not re.fullmatch(r'(?:ARRAY\s*\[.+\]\s*OF\s+)?(?:POINTER\s+TO\s+|REFERENCE\s+TO\s+)?[A-Z_]\w*(?:\.\w+)*(?:\s*\([^;:]*\)|\s*\[[^;:]*\])?', specification, re.S):
                issues.append('Unsupported or malformed type specification: ' + specification[:120])
            for name in match[1].split(','):
                key = name.strip().upper()
                if key in symbols:
                    issues.append('Duplicate declaration: ' + key)
                symbols[key] = {'type': specification, 'scope': scope}
                if constant:
                    symbols[key]['constant'] = True
                    if ':=' in match[2]:
                        symbols[key]['initializer'] = match[2].split(':=', 1)[1].strip()
        if body.strip() and not body.rstrip().endswith(';'):
            issues.append('Missing declaration semicolon.')
    if re.search(r'\b(?:IMPLEMENTS|REFERENCE TO)\b', code, re.I):
        # These are syntactically valid; complete type/lifetime analysis is not
        # implemented here and must never be silently reported as verified.
        issues.append('Inheritance, interface, pointer/reference semantics are not fully supported.')
    header = re.sub(r'\{[^}]*\}', '', code).strip()
    if header and not re.match(r'^(?:PROGRAM|FUNCTION_BLOCK|FUNCTION|METHOD|PROPERTY|INTERFACE|TYPE|VAR(?:_\w+)?)\b', header, re.I):
        issues.append('Unsupported object declaration header.')
    if re.match(r'^TYPE\b', header, re.I):
        typedef = re.fullmatch(r'TYPE\s+\w+\s*:\s*(.*?)\s*END_TYPE\s*;?', header, re.I | re.S)
        if not typedef:
            issues.append('Malformed TYPE declaration.')
        elif typedef[1].lstrip().startswith('('):
            enum = enum_definition(header)
            if not enum:
                issues.append('Malformed enum declaration.')
            elif enum['status']!='parsed':
                issues.append(enum['reason'])
        elif not fields and not alias_target(header):
            issues.append('Alias/subrange TYPE declarations require additional type resolution.')
    from .constant_bounds import resolve_shapes
    resolve_shapes(symbols)
    return symbols, issues


def review_candidate(candidate, resolve=None, resolve_member=None, *, resolve_qualified=None, resolve_settings=None):
    from .conditional_compilation import preprocess_candidate
    from .compiler_context import source_version_requirements
    version_requirements=source_version_requirements(candidate)
    candidate, preprocessing=preprocess_candidate(candidate)
    implementation = candidate.get('implementation', '') or ''
    parsed = parse_implementation(implementation)
    from .plc_syntax import syntax_findings
    findings = syntax_findings(candidate)
    findings.extend({**f, 'object': candidate.get('name', '')} for f in parsed['findings'])
    symbols, unresolved = declarations(candidate.get('enclosing_declaration', '') or '')
    own, problems = declarations(candidate.get('declaration', '') or '')
    symbols.update(own)  # method-local variables shadow the enclosing scope
    unresolved.extend(problems)
    unresolved.extend(v['reason'] for v in preprocessing.values() if v['status']!='processed')
    cache = {}
    pointer_used = False
    owner_match = re.search(r'\bFUNCTION_BLOCK\s+(?:(?:FINAL|ABSTRACT)\s+)*([A-Za-z_]\w*)',
                            _st_code(candidate.get('enclosing_declaration') or candidate.get('declaration', '')), re.I)
    owner = owner_match[1].upper() if owner_match else ''

    def is_this(node):
        return (node.kind == 'deref' and len(node.children) == 1
                and node.children[0].kind == 'name' and node.children[0].token.value.upper() == 'THIS')

    def method_signature(child, name, internal=False):
        if not child:
            return None
        version_requirements.extend({**r,'dependency':name} for r in source_version_requirements(child))
        child, child_preprocessing=preprocess_candidate(child)
        unresolved.extend(v['reason'] for v in child_preprocessing.values() if v['status']!='processed')
        declaration = _st_code(child.get('declaration', ''))
        signature = re.match(r'\s*METHOD\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL|ABSTRACT)\s+)*([A-Za-z_]\w*)\b', declaration, re.I)
        if not signature or signature[1].upper() != name.upper():
            unresolved.append('Invalid method signature: ' + name)
            return None
        if re.search(r'\bABSTRACT\b', declaration, re.I):
            unresolved.append('Abstract method dispatch is not verified: ' + name)
        if child.get('inherited_from') and re.search(r'\bPRIVATE\b',declaration,re.I):
            add('Inherited PRIVATE method is not accessible from the derived type.',rule='semantic-access')
        if not internal and re.search(r'\b(?:PRIVATE|PROTECTED|INTERNAL)\b', declaration, re.I):
            unresolved.append('Method access visibility needs enclosing-type validation: ' + name)
        parameters, problems = declarations(declaration)
        unresolved.extend(problems)
        return child, parameters

    def add(message, node=None, rule='semantic-type'):
        pos = node.token.pos if node else 0
        findings.append({'rule': rule, 'severity': 'error', 'object': candidate.get('name', ''),
                         'area': 'implementation' if node else 'declaration',
                         'line': implementation.count('\n', 0, pos) + 1 if node else 1,
                         'message': message})

    def definition(name, chain=()):
        key = name.upper()
        if key in chain or len(chain)>=16:
            unresolved.append('Cyclic or excessive inheritance chain: ' + name)
            return None
        if key in cache:
            return cache[key]
        if len(cache) >= 32:
            unresolved.append('Type resolution budget exceeded.')
            return None
        cache[key] = None
        obj = resolve(name) if resolve else None
        if not obj:
            unresolved.append('Unresolved declaration/library symbol: ' + name)
            return None
        if obj.get('opaque_type'):
            cache[key] = (obj, {})
            return cache[key]
        version_requirements.extend({**r,'dependency':name} for r in source_version_requirements(obj))
        version_requirements.extend({**r,'dependency':name} for r in obj.get('source_version_requirements',[]))
        obj, obj_preprocessing=preprocess_candidate(obj)
        unresolved.extend(v['reason'] for v in obj_preprocessing.values() if v['status']!='processed')
        values, problems = declarations(obj.get('declaration', ''))
        unresolved.extend(name + ': ' + p for p in problems)
        base = re.search(r'\bFUNCTION_BLOCK\s+(?:(?:FINAL|ABSTRACT)\s+)*\w+\s+EXTENDS\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\b', _st_code(obj.get('declaration','')), re.I)
        if base:
            inherited=definition(base[1],(*chain,key))
            if inherited:
                if not re.search(r'\bFUNCTION_BLOCK\b',_st_code(inherited[0].get('declaration','')),re.I):
                    add('Base type must be a function block.',rule='semantic-inheritance')
                if re.search(r'\bFUNCTION_BLOCK\s+FINAL\b',_st_code(inherited[0].get('declaration','')),re.I):
                    add('Cannot extend a FINAL function block.',rule='semantic-inheritance')
                values={**inherited[1],**values}
            else:
                unresolved.append('Base function block could not be resolved: '+base[1])
        cache[key] = (obj, values)
        return cache[key]

    def simple(spec):
        return re.sub(r'\s*[\[(].*', '', spec or '').strip()

    def expand_type(spec, seen=()):
        if not spec or not re.fullmatch(r'[A-Z_]\w*(?:\.[A-Z_]\w*)*', spec) or spec in SCALARS:
            return spec
        if spec in seen or len(seen) >= 16:
            unresolved.append('Cyclic or excessive alias chain: ' + spec)
            return spec
        found = definition(spec)
        if not found:
            return spec
        target = found[0].get('public_alias') or alias_target(_st_code(found[0].get('declaration', '')))
        return expand_type(target, (*seen, spec)) if target else spec

    def compatible(target, source, node):
        if not target or not source:
            return
        target, source = expand_type(target), expand_type(source)
        target_array, source_array = array_type(target), array_type(source)
        if target_array or source_array:
            if not target_array or not source_array:
                add('Array and non-array types are incompatible.', node)
                return
            if len(target_array[0]) != len(source_array[0]):
                add('Array ranks are incompatible.', node)
            if expand_type(target_array[1]) != expand_type(source_array[1]):
                add('Array element types are incompatible.', node)
            if target_array[0] != source_array[0] and not all(d == '*' for d in target_array[0]):
                if all(re.fullmatch(r'-?\d+\.\.-?\d+', d) for d in target_array[0] + source_array[0]):
                    add('Array bounds are incompatible.', node)
                else:
                    unresolved.append('Array bounds compatibility requires exact shape evidence.')
            return
        for spec in (target, source):
            base = simple(spec)
            if base in {'T_AMSNETID', 'T_IPV4ADDR'}:
                found = definition(base)
                alias = found[0].get('public_alias') if found else None
                if alias:
                    if spec == target:
                        target = alias
                    if spec == source:
                        source = alias
        # A byte array's base address is used as a byte buffer in official
        # TF6310 examples. This does not verify cbLen or runtime memory safety.
        if (target == 'POINTER TO BYTE' and
                re.fullmatch(r'POINTER TO ARRAY\s*\[[^\]]+\]\s*OF\s*BYTE', source)):
            return
        a, b = simple(target), simple(source)
        if b.startswith('POINTER TO ') and a in {'PVOID', '__XWORD', 'LWORD'}:
            return
        if b.startswith('POINTER TO ') and a == 'DWORD':
            unresolved.append('Address assignment to DWORD requires a verified 32-bit target; use PVOID or __XWORD.')
            return
        if a == b or a in INTS | REALS | BITS and b in INTS | REALS | BITS:
            return
        if '__UXINT' in {a, b}:
            unresolved.append('Native unsigned integer conversion requires actual target-width evidence; retain __UXINT for XSIZEOF.')
            return
        for typ, other in ((a,b),(b,a)):
            found=cache.get(typ.upper())
            if found and (found[0].get('public_kind')=='struct' or
                          re.search(r'\bSTRUCT\b',_st_code(found[0].get('declaration','')),re.I)) and other in SCALARS:
                add(f'STRUCT {typ} cannot be compared with or assigned from/to scalar {other}; use the documented member or a value of the same structure type.',node,'semantic-type')
                return
        if a not in SCALARS or b not in SCALARS:
            unresolved.append('Type conversion requires interface/alias evidence: ' + source + ' -> ' + target)
            return
        add(f'Incompatible assignment/argument: {source} -> {target}.', node)

    def assign(target, expression, node):
        target = expand_type(target)
        limits = subrange(target)
        if limits:
            number = constant_integer(expression)
            if limits[1] is None:
                unresolved.append('Subrange bounds require constant resolution: ' + target)
            elif limits[1] > limits[2] or not all(fits_integer(n, limits[0]) for n in limits[1:]):
                add('Invalid subrange bounds for base type.', node, 'semantic-range')
            elif number is not None and not limits[1] <= number <= limits[2]:
                add('Constant is outside the declared subrange.', node, 'semantic-range')
        if simple(target).startswith('POINTER TO ') or simple(target) == 'PVOID':
            if constant_integer(expression) == 0:
                return
        compatible(target, infer(expression), node)
        length_target = target
        if simple(target) in {'T_AMSNETID', 'T_IPV4ADDR'}:
            found = definition(simple(target))
            if found:
                length_target = found[0].get('public_alias', target)
        string_size = string_spec(length_target)
        literal = expression.token.value
        # Exact ASCII literals only: do not guess encoded byte lengths of
        # Unicode or IEC $ escapes. Those need separate encoding evidence.
        if (string_size and expression.kind == 'literal' and expression.token.kind == 'string'
                and literal.startswith("'" if string_size[0] == 'STRING' else '"') and '$' not in literal and literal.isascii()
                and len(literal[1:-1]) > int(string_size[1])):
            add('String literal exceeds destination capacity; truncation would lose data.', expression, 'semantic-range')
        number = constant_integer(expression)
        widths = {'SINT': (8, True), 'USINT': (8, False), 'INT': (16, True),
                  'UINT': (16, False), 'DINT': (32, True), 'UDINT': (32, False),
                  'LINT': (64, True), 'ULINT': (64, False)}
        if simple(target) in widths and number is not None:
            width, signed = widths[simple(target)]
            minimum, maximum = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
            if not minimum <= number <= maximum:
                add('Integer constant outside destination range.', node, 'semantic-range')

    def check_writable(node):
        # Member/index selection retains the containing variable's constness.
        # Dereferencing a constant pointer does not make its pointee constant.
        root = node
        while root.kind in {'member', 'index'}:
            root = root.children[0]
        if root.kind == 'name' and symbols.get(root.token.value.upper(), {}).get('constant'):
            add('Cannot write to a CONSTANT variable or its stored members.', node, 'semantic-constant-write')

    def member_type(parent, member, node):
        found = definition(parent)
        if not found:
            return ''
        obj, values = found
        if obj.get('public_kind')=='struct':
            typ=obj.get('public_members',{}).get(member.upper())
            if not typ:
                add('Unknown public structure member: '+parent+'.'+member,node,'semantic-member')
                return ''
            if simple(typ) not in SCALARS:
                child=definition(typ)
                if not child or any(child[0].get(k)!=obj.get(k) for k in ('library','version')):
                    unresolved.append('Public member dependency must belong to the same resolved library/version: '+parent+'.'+member)
                    return ''
            return typ
        if obj.get('opaque_type'):
            unresolved.append('Opaque library type member requires interface evidence: ' + parent + '.' + member)
            return ''
        if member.upper() in values:
            return values[member.upper()]['type']
        enum = re.search(r'\bTYPE\s+\w+\s*:\s*\((.*?)\)', _st_code(obj.get('declaration', '')), re.I | re.S)
        if enum and re.search(r'(?:^|,)\s*' + re.escape(member) + r'\b', enum[1], re.I):
            return parent.upper()
        if re.search(r'\bFUNCTION_BLOCK\b', obj.get('declaration', ''), re.I) and resolve_member:
            child = resolve_member(parent, member)
            signature = re.search(r'\bPROPERTY\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL)\s+)*\w+\s*:\s*(\w+)',
                                  (child or {}).get('declaration', ''), re.I)
            if signature:
                unresolved.append('Property getter/setter availability is not verified: ' + parent + '.' + member)
                return signature[1].upper()
        if re.search(r'\b(?:EXTENDS|IMPLEMENTS)\b', obj.get('declaration', ''), re.I):
            unresolved.append('Inherited member unresolved: ' + parent + '.' + member)
        else:
            add('Unknown member: ' + parent + '.' + member, node, 'semantic-member')
        return ''

    def infer(node):
        nonlocal pointer_used
        kind, value = node.kind, node.token.value.upper()
        if kind == 'assign':
            target, expression = node.children
            typ = infer(target)
            if value == 'REF=':
                unresolved.append('Reference binding/lifetime is not fully verified.')
            assign(typ, expression, node)
            return typ
        if kind == 'literal':
            if node.token.kind == 'string':
                return 'STRING' if node.token.value.startswith("'") else 'WSTRING'
            if value in {'TRUE', 'FALSE'}:
                return 'BOOL'
            integer = integer_literal(value)
            if integer and integer[1] and not fits_integer(integer[0], integer[1]):
                add('Integer literal is outside its explicit type range.', node, 'semantic-range')
            if not integer and re.match(r'(?:(?:' + '|'.join(WIDTHS) + r')#|(?:2|8|16)#)', value):
                add('Invalid or unsupported integer literal representation.', node, 'semantic-unresolved')
            if '#' in value:
                prefix = value.split('#')[0]
                date_error = date_literal_error(value)
                if date_error:
                    add(date_error, node, 'semantic-unresolved' if date_error.startswith('Unsupported') else 'semantic-range')
                prefix = DATE_ALIASES.get(prefix, prefix)
                return {'T': 'TIME', 'D': 'DATE', 'LT': 'LTIME'}.get(prefix, prefix if prefix in SCALARS else 'DINT')
            return 'LREAL' if '.' in value or 'E' in value else 'DINT'
        if kind == 'name':
            if value in symbols:
                return expand_type(symbols[value]['type'])
            # Function/method return names are writable identifiers.
            signature = re.search(r'\b(?:FUNCTION|METHOD|PROPERTY)\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL|ABSTRACT)\s+)*' + re.escape(value) + r'\s*:\s*(\w+)', candidate.get('declaration', ''), re.I)
            if signature:
                result_type = declared_return_type(_st_code(candidate.get('declaration', '')))
                if result_type:
                    if result_type.startswith('POINTER TO '):
                        pointer_used = True
                    return result_type
                unresolved.append('Return type requires additional interface evidence: ' + value)
                return ''
            found = definition(node.token.value)
            if found:
                return value
            return ''
        if kind == 'member':
            parent = infer(node.children[0])
            return member_type(parent, node.token.value, node) if parent else ''
        if kind == 'index':
            parent = infer(node.children[0])
            array = re.fullmatch(r'ARRAY\s*\[(.+)\]\s*OF\s*(.+)', parent, re.S)
            for index in node.children[1:]:
                typ = infer(index)
                if typ and typ not in INTS:
                    add('Array index must be an integer.', index)
            if not array:
                if simple(parent) in {'STRING', 'WSTRING'}:
                    return 'BYTE' if simple(parent) == 'STRING' else 'WORD'
                if parent:
                    add('Indexing a non-array value: ' + parent, node)
                return ''
            ranges = array[1].split(',')
            if len(ranges) != len(node.children) - 1:
                add('Array rank/index count mismatch.', node)
            for bounds, index in zip(ranges, node.children[1:]):
                match = re.fullmatch(r'\s*([+-]?\d+)\s*\.\.\s*([+-]?\d+)\s*', bounds)
                number = constant_integer(index)
                if match and number is not None and not int(match[1]) <= number <= int(match[2]):
                    add('Constant array index outside declared bounds: ' + str(number), index, 'semantic-array-bounds')
                elif not match:
                    unresolved.append('Symbolic/open array bounds require resolution: ' + bounds)
            return array[2]
        if kind == 'unary':
            typ = infer(node.children[0])
            if value == 'NOT' and typ and typ not in {'BOOL'} | BITS:
                add('NOT requires BOOL or bit string.', node)
            if value in {'+', '-'} and typ in {'BOOL', 'STRING', 'WSTRING'}:
                add('Unary arithmetic has non-numeric operand.', node)
            return typ
        if kind == 'binary':
            left, right = (infer(c) for c in node.children)
            if value == 'MOD' and any(t and t in SCALARS and t not in INTS | BITS for t in (left, right)):
                add('MOD requires integer or bit-string operands.', node, 'semantic-operator')
            if value in {'/', 'MOD'} and constant_integer(node.children[1]) == 0:
                add('Literal zero divisor is unsafe; runtime behavior is not verified.', node, 'semantic-zero-divisor')
            if value in {'=', '<>', '<', '>', '<=', '>='}:
                compatible(left, right, node)
                return 'BOOL'
            if value in {'AND', 'OR', 'XOR', 'AND_THEN', 'OR_ELSE', '&'}:
                if left and left not in {'BOOL'} | BITS or right and right not in {'BOOL'} | BITS:
                    add('Boolean/bitwise operator has incompatible operand.', node)
                if value in {'AND_THEN', 'OR_ELSE'} and (left and left != 'BOOL' or right and right != 'BOOL'):
                    add('Short-circuit operator requires BOOL operands.', node)
                return left or right
            if left in {'STRING', 'WSTRING', 'BOOL'} or right in {'STRING', 'WSTRING', 'BOOL'}:
                add('Arithmetic operator has non-numeric operand.', node)
            elif left and right and not (left in INTS | REALS | BITS and right in INTS | REALS | BITS):
                if not (left == right and left in {'TIME', 'LTIME'} and value in {'+', '-'}):
                    unresolved.append('Operator overload is not verified: ' + left + ' ' + value + ' ' + right)
            return left or right
        if kind == 'call':
            target, *args = node.children
            name = target.token.value.upper()
            if target.kind == 'name' and name not in symbols:
                from .builtin_calls import infer_builtin
                def size_of(operand):
                    from .compiler_context import layout_type, declaration_layout
                    if operand.kind == 'name' and operand.token.value.upper() not in symbols:
                        typ = operand.token.value.upper()
                    else:
                        typ = infer(operand)
                    def snapshot(typename):
                        found = definition(typename)
                        return declaration_layout(found[0].get('declaration', ''), declarations) if found else None
                    layout = layout_type(typ, snapshot)
                    return layout.get('size') if layout.get('status') == 'calculated' else None
                builtin_type = infer_builtin(name, args, node, infer, size_of, add, unresolved)
                if builtin_type is not None:
                    return builtin_type
            if target.kind == 'name' and name in {'LOWER_BOUND', 'UPPER_BOUND'}:
                if len(args) != 2 or any(arg.kind == 'argument' for arg in args):
                    add(name + ' requires an array and a dimension as two positional arguments.', node, 'semantic-call')
                    return 'DINT'
                array_type = infer(args[0])
                dimension_type = infer(args[1])
                array = re.fullmatch(r'ARRAY\s*\[([^\]]+)\]\s*OF\s*(.+)', array_type, re.S)
                if array_type and not array:
                    add(name + ' requires an array argument.', args[0], 'semantic-call')
                if dimension_type and dimension_type not in INTS:
                    add('Array dimension must be an integer.', args[1], 'semantic-call')
                dimension = constant_integer(args[1])
                if array and dimension is not None and not 1 <= dimension <= len(array[1].split(',')):
                    add('Array dimension is outside the declared rank.', args[1], 'semantic-array-bounds')
                return 'DINT'
            if target.kind == 'name' and name == 'ADR':
                pointer_used = True
                if len(args) != 1 or args[0].kind not in {'name', 'member', 'index', 'deref'}:
                    add('ADR requires one addressable variable, not a literal/expression or named argument.', node, 'semantic-call')
                    return ''
                operand = args[0]
                if operand.kind == 'name' and operand.token.value.upper() not in symbols:
                    unresolved.append('ADR target must resolve to a variable in the current scope.')
                    return ''
                base = infer(operand)
                if not base:
                    return ''
                # Preserve the pointee type. Do not turn all addresses into
                # integers, which would silently admit incompatible pointers.
                return 'POINTER TO ' + base
            conversion = re.fullmatch(r'(?:([A-Z][A-Z0-9]*)_)?TO_([A-Z][A-Z0-9]*)', name)
            if (target.kind == 'name' and conversion
                    and conversion[2] in SCALARS - {'PVOID', '__XWORD'}
                    and (conversion[1] is None or conversion[1] in SCALARS - {'PVOID', '__XWORD'})):
                if len(args) != 1 or args[0].kind == 'argument':
                    add('Conversion requires one positional argument.', node, 'semantic-call')
                for arg in args:
                    infer(arg.children[1] if arg.kind == 'argument' else arg)
                return conversion[2]
            qualified_name=[]
            root=target
            while root.kind=='member':
                qualified_name.insert(0,root.token.value)
                root=root.children[0]
            qualified_obj=None
            if qualified_name and root.kind=='name' and root.token.value.upper() not in symbols and resolve_qualified:
                qualified_name.insert(0,root.token.value)
                qualified_obj=resolve_qualified('.'.join(qualified_name))
            if qualified_obj:
                found=definition('.'.join(qualified_name))
                if found and not re.search(r'\bFUNCTION\b',_st_code(found[0].get('declaration','')),re.I):
                    add('Qualified call requires a function or declared FB instance.',node,'semantic-call')
            elif target.kind == 'member':
                parent_type = infer(target.children[0])
                child = resolve_member(parent_type, target.token.value) if parent_type and resolve_member else None
                found = method_signature(child, target.token.value,
                                         internal=bool(owner and is_this(target.children[0])))
                if not found:
                    unresolved.append('Unresolved method signature: ' + parent_type + '.' + target.token.value)
            else:
                # A local instance shadows an unqualified method. Only an
                # absent local symbol may bind to the enclosing FB's method.
                child = resolve_member(owner, target.token.value) if owner and name not in symbols and resolve_member else None
                if child:
                    found = method_signature(child, target.token.value, internal=True)
                else:
                    target_type = symbols.get(name, {}).get('type', name)
                    found = definition(target_type)
                    if found and not re.search(r'\b(?:FUNCTION_BLOCK|FUNCTION)\b', _st_code(found[0].get('declaration', '')), re.I):
                        add('Call target is not a function or FB instance: ' + name, node, 'semantic-call')
            if not found:
                for arg in args:
                    infer(arg.children[1] if arg.kind == 'argument' else arg)
                return ''
            obj, parameters = found
            inputs = [(n, p) for n, p in parameters.items() if p['scope'] in {'VAR_INPUT', 'VAR_IN_OUT'}]
            seen = set()
            supplied = {}
            for index, arg in enumerate(args):
                if arg.kind == 'argument':
                    arg_name = arg.token.value.upper()
                    parameter = parameters.get(arg_name)
                    direction, expression = arg.children
                    if not parameter or parameter['scope'] not in ({'VAR_OUTPUT'} if direction == '=>' else {'VAR_INPUT', 'VAR_IN_OUT'}):
                        add('Unknown parameter or invalid argument direction: ' + arg_name, arg, 'semantic-call')
                        parameter = None
                else:
                    expression = arg
                    if index >= len(inputs):
                        add('Too many positional arguments.', arg, 'semantic-call')
                        parameter, arg_name = None, str(index)
                    else:
                        arg_name, parameter = inputs[index]
                if arg_name in seen:
                    add('Duplicate argument: ' + arg_name, arg, 'semantic-call')
                seen.add(arg_name)
                value_type = infer(expression)
                if parameter:
                    if parameter['scope'] in {'VAR_INPUT', 'VAR_IN_OUT'}:
                        supplied[arg_name] = expression
                    if arg.kind == 'argument' and arg.children[0] == '=>':
                        check_writable(expression)
                        if expression.kind not in {'name', 'member', 'index', 'deref'}:
                            add('Output argument requires a writable variable.', expression, 'semantic-call')
                        compatible(value_type, parameter['type'], expression)
                    elif parameter['scope'] == 'VAR_IN_OUT':
                        target_type = expand_type(parameter['type'])
                        readonly_string = (parameter.get('constant') and
                                           simple(target_type) in {'STRING', 'WSTRING'})
                        formal_string = string_spec(target_type)
                        actual_string = string_spec(value_type)
                        if (not parameter.get('constant') and formal_string and actual_string and
                                formal_string[0] == actual_string[0] and
                                actual_string[1] < formal_string[1]):
                            add('String variable is too short for writable VAR_IN_OUT parameter.', expression, 'semantic-call')
                        # References cannot use numeric value conversions: no
                        # temporary copy is created for a pass-through argument.
                        if (target_type in INTS | REALS | BITS | {'BOOL'} and
                                value_type in INTS | REALS | BITS | {'BOOL'} and
                                target_type != value_type):
                            add(f'VAR_IN_OUT requires matching storage types: {value_type} -> {target_type}.', expression, 'semantic-call')
                        else:
                            compatible(target_type, value_type, expression)
                        if expression.kind not in {'name', 'member', 'index', 'deref'} and not (
                                readonly_string and expression.kind == 'literal' and
                                expression.token.kind == 'string'):
                            add('VAR_IN_OUT requires a variable, not an expression.', expression, 'semantic-call')
                        if not parameter.get('constant'):
                            check_writable(expression)
                        elif (not readonly_string and expression.kind == 'name' and
                              symbols.get(expression.token.value.upper(), {}).get('constant')):
                            settings=resolve_settings() if resolve_settings else None
                            replaced=settings.get('replace_constants') if settings else None
                            if replaced is True:
                                add('Replace constants is enabled; this constant has no verified reference storage.',expression,'semantic-call')
                            elif replaced is not False:
                                unresolved.append('Non-string constant passed by reference requires evidence of the Replace constants compiler option.')
                    else:
                        assign(parameter['type'], expression, expression)
            for param_name, parameter in parameters.items():
                if parameter['scope'] == 'VAR_IN_OUT' and param_name not in seen:
                    add('Missing VAR_IN_OUT argument: ' + param_name, node, 'semantic-call')
            knowledge = obj.get('interface_knowledge') or {}
            if knowledge.get('kind') == 'memory':
                pointer_used = True
                if obj.get('name', '').upper() == 'MEMCPY':
                    buffers = [supplied.get(p) for p in knowledge['pointers']]
                    if all(p and p.kind == 'call' and len(p.children) == 2
                           and p.children[0].token.value.upper() == 'ADR'
                           and p.children[1].kind == 'name' for p in buffers):
                        if buffers[0].children[1].token.value.upper() == buffers[1].children[1].token.value.upper():
                            add('MEMCPY source and destination overlap; this memory operation is undefined.', node, 'semantic-range')
                for required, _ in knowledge['inputs']:
                    if required.upper() not in supplied:
                        add('Missing memory function argument: ' + required, node, 'semantic-call')
                length = supplied.get(knowledge['length'])
                count = constant_integer(length) if length else None
                from .compiler_context import layout_type
                from .type_contract import array_type as memory_array_type

                def memory_size(operand):
                    # Only statically known scalar/array layouts; never guess ABI.
                    typ = (operand.token.value.upper() if operand.kind == 'name'
                           and operand.token.value.upper() not in symbols else infer(operand))
                    return layout_type(typ, lambda _: None).get('size')

                if (length and length.kind == 'call' and len(length.children) == 2
                        and length.children[0].kind == 'name'
                        and length.children[0].token.value.upper() == 'SIZEOF'
                        and 'SIZEOF' not in symbols):
                    count = memory_size(length.children[1])
                for pointer_name in knowledge['pointers']:
                    pointer = supplied.get(pointer_name)
                    if pointer and constant_integer(pointer) == 0:
                        add('Memory function requires non-null address.', pointer, 'semantic-range')
                    if (pointer and pointer.kind == 'call' and len(pointer.children) == 2
                            and pointer.children[0].token.value.upper() == 'ADR'):
                        operand = pointer.children[1]
                        if pointer_name == 'DESTADDR':
                            check_writable(operand)
                        capacity = memory_size(operand)
                        if operand.kind == 'index':
                            # ADR(a[i]) addresses the remaining contiguous array,
                            # not merely one element. Unknown indices stay unknown.
                            capacity = None
                            array = memory_array_type(infer(operand.children[0]))
                            if array and len(array[0]) == 1 and len(operand.children) == 2:
                                bounds = re.fullmatch(r'(-?\d+)\.\.(-?\d+)', array[0][0])
                                index = constant_integer(operand.children[1])
                                element = layout_type(array[1], lambda _: None).get('size')
                                if (bounds and index is not None and element is not None
                                        and int(bounds[1]) <= index <= int(bounds[2])):
                                    capacity = (int(bounds[2]) - index + 1) * element
                        if capacity is not None and count is not None and count > capacity:
                            add('Memory byte count exceeds addressed variable capacity.', length, 'semantic-range')
            if knowledge.get('kind') == 'buffer':
                pointer = supplied.get(knowledge['pointer'])
                length = supplied.get(knowledge['length'])
                if (pointer and length and pointer.kind == 'call' and
                        pointer.children[0].kind == 'name' and
                        pointer.children[0].token.value.upper() == 'ADR' and
                        len(pointer.children) == 2):
                    value_type = infer(pointer.children[1])
                    bounds = re.fullmatch(r'ARRAY\s*\[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\]\s*OF\s*BYTE', value_type)
                    byte_count = constant_integer(length)
                    if bounds and byte_count is not None and byte_count > int(bounds[2]) - int(bounds[1]) + 1:
                        add('Buffer byte count exceeds supplied BYTE array capacity.', length, 'semantic-range')
            return_type = obj.get('return_type') or declared_return_type(_st_code(obj.get('declaration', '')))
            if not return_type and re.search(r'\b(?:FUNCTION|METHOD)\s+[^\r\n]*:', _st_code(obj.get('declaration', '')), re.I):
                unresolved.append('Return type requires additional interface evidence: ' + name)
                return ''
            if return_type and return_type.startswith('POINTER TO '):
                pointer_used = True
            return return_type or 'VOID'
        if kind == 'deref':
            if owner and is_this(node):
                return owner
            pointer_used = True
            parent = infer(node.children[0])
            if parent.startswith('POINTER TO '):
                unresolved.append('Pointer dereference requires address/lifetime evidence; type is known but access safety is not verified.')
                return parent[len('POINTER TO '):]
            unresolved.append('Pointer dereference semantics not verified.')
            return ''
        unresolved.append('Unsupported expression: ' + kind)
        return ''

    def visit(node, loop=False):
        if node.kind == 'assign':
            lhs, rhs = node.children
            check_writable(lhs)
            if node.token.value.upper() == 'REF=':
                unresolved.append('Reference binding/lifetime is not fully verified.')
            assign(infer(lhs), rhs, node)
        elif node.kind in {'branch', 'while', 'repeat'}:
            condition, *body = node.children
            typ = infer(condition)
            if typ and typ != 'BOOL':
                add('Condition must be BOOL, not ' + typ, condition)
            for child in body:
                visit(child, loop or node.kind in {'while', 'repeat'})
        elif node.kind == 'for':
            check_writable(node.children[0])
            for child in node.children[:4]:
                typ = infer(child)
                if typ and typ not in INTS:
                    add('FOR control and bounds must be integer.', child)
            if constant_integer(node.children[3]) == 0:
                add('FOR step cannot be zero.', node)
            for child in node.children[4:]:
                visit(child, True)
        elif node.kind == 'case':
            selector_type = infer(node.children[0])
            if selector_type in SCALARS - INTS - BITS:
                add('CASE selector must be an integer or enum.', node.children[0])
            for child in node.children[1:]:
                if child.kind == 'case_branch':
                    for label in child.children[0]:
                        label_type = infer(label)
                        if selector_type and label_type:
                            compatible(selector_type, label_type, label)
                visit(child, loop)
        elif node.kind == 'case_branch':
            for label in node.children[0]:
                infer(label)
            for child in node.children[1:]:
                visit(child, loop)
        elif node.kind == 'if':
            for child in node.children:
                visit(child, loop)
        elif node.kind == 'control':
            if node.token.value.upper() in {'EXIT', 'CONTINUE'} and not loop:
                add('EXIT/CONTINUE outside a loop.', node)
        else:
            infer(node)

    if parsed['status'] == 'parsed':
        try:
            base = re.search(r'\bFUNCTION_BLOCK\s+(?:(?:FINAL|ABSTRACT)\s+)*\w+\s+EXTENDS\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\b', _st_code(candidate.get('enclosing_declaration') or candidate.get('declaration','')),re.I)
            if base:
                inherited=definition(base[1])
                if inherited:
                    if not re.search(r'\bFUNCTION_BLOCK\b',_st_code(inherited[0].get('declaration','')),re.I):
                        add('Base type must be a function block.',rule='semantic-inheritance')
                    if re.search(r'\bFUNCTION_BLOCK\s+FINAL\b',_st_code(inherited[0].get('declaration','')),re.I):
                        add('Cannot extend a FINAL function block.',rule='semantic-inheritance')
                    symbols={**inherited[1],**symbols}
                else:
                    unresolved.append('Base function block could not be resolved: '+base[1])
            own_alias = alias_target(_st_code(candidate.get('declaration', '')))
            if own_alias:
                expanded_alias = expand_type(own_alias)
                limits = subrange(expanded_alias)
                if limits and limits[1] is not None and (limits[1] > limits[2] or not all(fits_integer(n, limits[0]) for n in limits[1:])):
                    add('Invalid subrange bounds for base type.', rule='semantic-range')
            for symbol in symbols.values():
                expanded = expand_type(symbol['type'])
                limits = subrange(expanded)
                if limits and limits[1] is not None and (limits[1] > limits[2] or not all(fits_integer(n, limits[0]) for n in limits[1:])):
                    add('Invalid subrange bounds for base type.', rule='semantic-range')
                spec = re.sub(r'^ARRAY\s*\[.*\]\s*OF\s*', '', symbol['type'])
                if spec.startswith('POINTER TO '):
                    pointer_used = True
                    spec = spec[len('POINTER TO '):]
                base = simple(spec)
                if base not in SCALARS and re.fullmatch(r'[A-Z_]\w*', base):
                    definition(base)
            # Parse initializers as expressions as well; declaration-only
            # writes must not bypass type/range checking.
            raw_declaration = candidate.get('declaration', '') or ''
            declaration_code = _st_code(raw_declaration)
            for initial in re.finditer(r'\b(\w+)\s*:\s*([^;]*?)\s*:=([^;]*);', declaration_code):
                name = initial[1].upper()
                if name not in symbols:
                    continue
                expression = raw_declaration[initial.start(3):initial.end(3)]
                initializer = parse_implementation(name + ' := ' + expression + ';')
                if initializer['status'] != 'parsed':
                    unresolved.append('Initializer is malformed or unsupported: ' + name)
                else:
                    assign(symbols[name]['type'], initializer['nodes'][0].children[1], None)
            for node in parsed['nodes']:
                visit(node)
        except RecursionError:
            unresolved.append('Semantic nesting budget exceeded.')
    for message in dict.fromkeys(unresolved):
        add(message, rule='semantic-unresolved')
    from .compiler_context import layout_type, version_requirement, declaration_layout
    raw_declaration=candidate.get('declaration','') or ''
    layout=None
    struct_header=re.search(r'\bTYPE\s+(\w+)\s*:',_st_code(raw_declaration),re.I)
    if struct_header:
        def layout_snapshot(name):
            obj=candidate if name.upper()==struct_header[1].upper() else (resolve(name) if resolve else None)
            if not obj: return None
            return declaration_layout(obj.get('declaration',''),declarations)
        layout=layout_type(struct_header[1],layout_snapshot)
    long_types=bool(re.search(r'\b(?:LDATE|LDT|LTOD|LDATE_AND_TIME|LTIME_OF_DAY)\b',_st_code(raw_declaration),re.I))
    return {'approved': not findings, 'syntax_status': parsed['status'],
            'memory_layout':layout, 'preprocessing':preprocessing,
            'target_version_requirement':version_requirement(None) if long_types else None,
            'target_version_requirements':version_requirements,
            'target_compatibility_verified':False if version_requirements else None,
            'semantic_complete': not unresolved and parsed['status'] == 'parsed',
            'compiler_verified': False, 'findings': findings,
            'pointer_safety_verified': False if pointer_used else None,
            'pointer_scope': 'ADR/type compatibility only; address validity, lifetime, alignment, byte count and online-change safety are not verified.' if pointer_used else '',
            'scope': 'Offline supported ST statements and resolved declarations only; not full TwinCAT compiler semantics.'}


def constant_integer(node):
    if node.kind == 'literal':
        parsed = integer_literal(node.token.value)
        return parsed[0] if parsed else None
    if node.kind == 'unary' and node.token.value in {'+', '-'}:
        value = constant_integer(node.children[0])
        return value if node.token.value == '+' else -value if value is not None else None
    return None
