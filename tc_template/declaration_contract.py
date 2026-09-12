"""Bounded declaration checks; input has comments/literals/pragmas masked.

Official sources and unsupported cases are recorded in the coverage document.
This is not a replacement for compiler preprocessing or type resolution.
"""
import re
from .integer_literals import integer_literal, fits_integer

# Unambiguous ST scope/type/operator names from the official keyword list.
# IL mnemonics such as R/S are not generalized here without dialect context.
ST_KEYWORDS = set('''ABS ACOS ADD ADR AND ARRAY ASIN AT ATAN BITADR BOOL BY BYTE
CASE CONSTANT COS DATE DINT DIV DO DT DWORD ELSE ELSIF END_CASE END_FOR END_IF
END_REPEAT END_STRUCT END_TYPE END_VAR END_WHILE EQ EXIT EXP EXPT FALSE FOR
FUNCTION FUNCTION_BLOCK GE GT IF INDEXOF INT LE LINT LN LOG LREAL LT LTIME LWORD
MAX METHOD MIN MOD MOVE MUL MUX NE NOT OF OR PERSISTENT POINTER PROGRAM
REAL REFERENCE REPEAT RETAIN RETURN ROL ROR SEL SHL SHR SIN SINT SIZEOF SUPER
SQRT STRING STRUCT SUB TAN THEN THIS TIME TO TOD TRUE TRUNC TYPE UDINT UINT
ULINT UNTIL USINT VAR VAR_CONFIG VAR_EXTERNAL VAR_GLOBAL VAR_IN_OUT VAR_INPUT
VAR_OUTPUT VAR_STAT VAR_TEMP WHILE WORD WSTRING XOR ACTION END_ACTION
END_FUNCTION END_FUNCTION_BLOCK END_PROGRAM'''.split())


def declaration_findings(code):
    findings = []
    seen = set()

    def add(rule, pos, message):
        findings.append((rule, pos, message))

    for block in re.finditer(r'\b(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_GLOBAL|_TEMP|_STAT|_INST|_EXTERNAL)?)\b(.*?)\bEND_VAR\b', code, re.I | re.S):
        scope, body = block[1].upper(), block[2]
        qualifier = re.match(r'\s*((?:(?:CONSTANT|RETAIN|NON_RETAIN|PERSISTENT)\s+)*)', body, re.I)
        constant = bool(re.search(r'\bCONSTANT\b', qualifier[1], re.I))
        start = qualifier.end()
        for statement in re.finditer(r'([^;]+);', body[start:]):
            pos = block.start(2) + start + statement.start()
            match = re.fullmatch(r'\s*([^:]+?)\s*:\s*(.+)', statement[1], re.S)
            if not match:
                continue  # Other syntax/unsupported checks own this case.
            names = re.split(r'\s+AT\s+', match[1], maxsplit=1, flags=re.I)[0]
            for name in names.split(','):
                name = name.strip()
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                    add('syntax-identifier', pos, 'Invalid variable identifier: ' + name)
                elif name.upper() in ST_KEYWORDS:
                    add('syntax-keyword-identifier', pos, 'ST keyword cannot be used as a variable identifier: ' + name)
                elif name.upper() in seen:
                    add('syntax-duplicate-variable', pos, 'Duplicate variable in the same declaration scope: ' + name)
                seen.add(name.upper())
            initialized = ':=' in match[2]
            specification = match[2].split(':=', 1)[0].strip()
            array = re.fullmatch(r'ARRAY\s*\[([^\]]+)\]\s*OF\s*(.+)', specification, re.I | re.S)
            if array:
                dimensions = [part.strip() for part in array[1].split(',')]
                if '*' in dimensions and scope != 'VAR_IN_OUT':
                    add('syntax-open-array-scope', pos, 'Variable-length arrays require VAR_IN_OUT scope.')
                if '*' in dimensions and any(part != '*' for part in dimensions):
                    add('syntax-open-array-dimensions', pos, 'Variable-length array dimensions must all use *.')
                for dimension in dimensions:
                    limits = dimension.split('..')
                    if len(limits) != 2:
                        continue
                    parsed = [integer_literal(limit.strip()) for limit in limits]
                    if all(value is not None for value in parsed):
                        low, high = (value[0] for value in parsed)
                        if low > high or not all(fits_integer(value, 'DINT') for value in (low, high)):
                            add('syntax-array-bounds', pos, 'Fixed array bounds must be ordered DINT-range integers.')
                if array[2].strip().upper() == 'BIT':
                    add('syntax-array-bit', pos, 'BIT is not a permitted array element type.')
            if re.fullmatch(r'(?:POINTER|REFERENCE)\s+TO\s+BIT', specification, re.I):
                add('syntax-reference-bit', pos, 'Pointers and references to BIT are not permitted.')
            if constant and scope in {'VAR', 'VAR_INPUT', 'VAR_STAT', 'VAR_GLOBAL'} and not initialized:
                add('syntax-constant-initializer', pos, 'CONSTANT variable requires a declaration initializer.')
            if constant and scope == 'VAR_IN_OUT' and initialized:
                add('syntax-inout-initializer', pos, 'VAR_IN_OUT CONSTANT must not declare an initialization value.')
    return findings
