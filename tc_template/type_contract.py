"""Small structural type descriptions, not a compiler or memory-layout model."""
import re
from .integer_literals import integer_literal, fits_integer, WIDTHS


def alias_target(code):
    match = re.fullmatch(r'\s*TYPE\s+\w+\s*:\s*([^;]+);\s*END_TYPE\s*;?\s*', code, re.I | re.S)
    if not match or ':=' in match[1]:
        return None
    value = match[1].strip().upper()
    if re.fullmatch(r'(?:ARRAY\s*\[[^\]]+\]\s*OF\s*)?(?:POINTER\s+TO\s+)?[A-Z_]\w*(?:\.[A-Z_]\w*)*(?:\s*\([^;]+\)|\s*\[[^;]+\])?', value):
        return value
    return None


def array_type(spec):
    match = re.fullmatch(r'ARRAY\s*\[([^\]]+)\]\s*OF\s*(.+)', spec, re.I | re.S)
    return ([re.sub(r'\s+', '', part).upper() for part in match[1].split(',')], match[2].strip().upper()) if match else None


def subrange(spec):
    match = re.fullmatch(r'(SINT|USINT|INT|UINT|DINT|UDINT|LINT|ULINT|BYTE|WORD|DWORD|LWORD)\s*\(\s*(.+?)\s*\.\.\s*(.+?)\s*\)', spec, re.I)
    if not match:
        return None
    low, high = integer_literal(match[2]), integer_literal(match[3])
    return (match[1].upper(), low[0], high[0]) if low and high else (match[1].upper(), None, None)


def enum_definition(code):
    """Literal-valued enums; expressions stay unknown instead of guessed values."""
    match=re.fullmatch(r'\s*TYPE\s+(\w+)\s*:\s*\((.*?)\)\s*(\w+)?\s*(?::=\s*([\w.]+))?\s*;?\s*END_TYPE\s*;?\s*',code,re.I|re.S)
    if not match: return None
    base=(match[3] or 'INT').upper()
    if base not in WIDTHS:
        return {'status':'error','reason':'Invalid enumeration base type: '+base}
    entries=match[2].split(',')
    if len(entries)>4096: return {'status':'unknown','reason':'Enumeration member budget exceeded'}
    members={}
    value=0
    for entry in entries:
        member=re.fullmatch(r'\s*([A-Za-z_]\w*)\s*(?::=\s*(.+?))?\s*',entry,re.S)
        if not member: return {'status':'unknown','reason':'Malformed enumeration member'}
        name=member[1].upper()
        if name in members: return {'status':'error','reason':'Duplicate enum member: '+name}
        if member[2]:
            literal=integer_literal(member[2].strip())
            if literal is None: return {'status':'unknown','reason':'Enum initializer requires constant expression resolution: '+name}
            if literal[1] and not fits_integer(literal[0],literal[1]):
                return {'status':'error','reason':'Typed enum literal outside its own type range: '+name}
            value=literal[0]
        if not fits_integer(value,base):
            return {'status':'error','reason':'Enum member outside base type range: '+name}
        members[name]=value
        value+=1
    default=match[4]
    if default:
        parts=default.upper().split('.')
        if len(parts)>2 or (len(parts)==2 and parts[0]!=match[1].upper()) or parts[-1] not in members:
            return {'status':'error','reason':'Unknown enum default member: '+default}
    return {'status':'parsed','base':base,'members':members,
            'default':default.upper().split('.')[-1] if default else next(iter(members))}
