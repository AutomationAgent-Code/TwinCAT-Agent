"""Official ST call-form operators; subset typing, never runtime evaluation.

Source: https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528853899.html
TIME(): 2529365899; SIZEOF: 2528896907; XSIZEOF: 13614027275.
Unknown target-dependent overloads remain explicit capability gaps.
"""
NUMERIC = frozenset('SINT USINT INT UINT DINT UDINT LINT ULINT REAL LREAL BYTE WORD DWORD LWORD'.split())
INTEGER = NUMERIC - {'REAL', 'LREAL'}
UNARY_MATH = frozenset('ACOS ASIN ATAN COS SIN TAN EXP LN LOG SQRT'.split())
NAMES = frozenset({'TIME', 'SIZEOF', 'XSIZEOF', 'MOVE', 'ABS', 'EXPT', 'TRUNC', 'TRUNC_INT',
                   'MIN', 'MAX', 'LIMIT', 'SEL', 'MUX', 'SHL', 'SHR', 'ROL', 'ROR'}) | UNARY_MATH


def infer_builtin(name, args, node, infer, size_of, add, unresolved):
    """None means not a handled built-in; empty type means failed validation."""
    if name not in NAMES:
        return None
    def fail(message):
        add(name + ': ' + message, node, 'semantic-call')
        return ''
    if any(a.kind == 'argument' for a in args):
        unresolved.append(name + ': named-argument overload requires additional operator evidence; use documented positional form.')
        return ''
    arity = {'TIME': 0, 'SIZEOF': 1, 'XSIZEOF': 1, 'MOVE': 1, 'ABS': 1, 'EXPT': 2,
             'TRUNC': 1, 'TRUNC_INT': 1, 'LIMIT': 3, 'SEL': 3,
             'SHL': 2, 'SHR': 2, 'ROL': 2, 'ROR': 2}
    if name in UNARY_MATH:
        arity[name] = 1
    if (name in arity and len(args) != arity[name]) or (name in {'MIN', 'MAX', 'MUX'} and len(args) < 2):
        return fail('invalid argument count')
    if name == 'TIME':
        return 'TIME'
    if name in {'SIZEOF', 'XSIZEOF'}:
        if args[0].kind not in {'name', 'member', 'index', 'deref'}:
            return fail('requires a variable or type name')
        size = size_of(args[0])
        if size is None:
            unresolved.append(name + ': actual layout/target evidence unavailable; no byte size guessed.')
            return ''
        if name == 'XSIZEOF':
            return '__UXINT'  # Symbolic native unsigned type, never guess width.
        return 'USINT' if size < 256 else 'UINT' if size < 65536 else 'UDINT' if size < 4294967296 else 'ULINT'
    types = [infer(a) for a in args]
    if not all(types):
        return ''
    if name == 'MOVE':
        return types[0]
    if name in UNARY_MATH | {'ABS', 'EXPT', 'TRUNC', 'TRUNC_INT'}:
        if any(t not in NUMERIC for t in types):
            return fail('numeric operand required')
        if name in {'TRUNC', 'TRUNC_INT'}:
            if types[0] not in {'REAL', 'LREAL'}:
                return fail('floating-point operand required')
            return 'DINT' if name == 'TRUNC' else 'INT'
        if name == 'ABS':
            return types[0]
        return 'LREAL' if name == 'EXPT' or 'LREAL' in types else 'REAL'
    if name in {'SHL', 'SHR', 'ROL', 'ROR'}:
        if types[0] not in INTEGER or types[1] not in INTEGER:
            return fail('integer/bit operand and integer shift count required')
        if name in {'ROL', 'ROR'} and types[0] not in {'BYTE', 'WORD', 'DWORD', 'LWORD'}:
            return fail('rotation operand must be BYTE/WORD/DWORD/LWORD')
        return types[0]
    if name == 'SEL' and types[0] != 'BOOL':
        return fail('selector must be BOOL')
    if name == 'MUX' and types[0] not in INTEGER:
        return fail('selector must be integer/bit type')
    values = types[1:] if name in {'SEL', 'MUX'} else types
    operands = args[1:] if name in {'SEL', 'MUX'} else args
    if len(set(values)) != 1:
        # Untyped integer constants can adopt the common variable type when
        # representable. Do not reject LIMIT(0, nUdint, 100) as a library gap.
        from .integer_literals import integer_literal, fits_integer
        literals = [integer_literal(a.token.value) if a.kind == 'literal' and '#' not in a.token.value
                    else None for a in operands]
        concrete = {typ for typ, literal in zip(values, literals) if literal is None}
        if len(concrete) == 1:
            common = next(iter(concrete))
            if common in INTEGER and all(lit is None or fits_integer(lit[0], common) for lit in literals):
                return common
        unresolved.append(name + ': mixed-type overload requires explicit common-type conversion.')
        return ''
    return values[0]
