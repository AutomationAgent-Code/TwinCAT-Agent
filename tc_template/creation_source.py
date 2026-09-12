"""Normalize the source candidate once, before review and native creation."""
import re


def declaration_for_creation(kind, name, declaration, return_type=''):
    if str(kind).casefold() != 'function':
        return declaration
    if not re.fullmatch(r'[A-Za-z_]\w*', name):
        raise ValueError('Function name must be a valid identifier')
    if not return_type or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\d+\))?', return_type):
        raise ValueError('Function requires an explicit valid return_type')
    from .lint import _st_code
    code = _st_code(declaration).strip()
    signature = re.match(r'FUNCTION\s+([A-Za-z_]\w*)\s*:\s*([^\r\n]+)', code, re.I)
    if signature:
        if signature[1].casefold()!=name.casefold() or signature[2].strip().casefold()!=return_type.casefold():
            raise ValueError('Function declaration signature differs from name/return_type')
        return declaration
    if code and not re.match(r'VAR(?:_INPUT|_OUTPUT|_IN_OUT|_TEMP)?\b',code,re.I):
        raise ValueError('Function declaration must contain its signature or VAR sections')
    return f'FUNCTION {name} : {return_type}\n{declaration}'
