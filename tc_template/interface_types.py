"""Small, explicit grammar for interface types we can preserve losslessly."""
import re


def string_spec(value):
    """Return (kind, capacity) for literal sizes and the official default 80."""
    match = re.fullmatch(r'(W?STRING)(?:\s*\(\s*(\d+)\s*\)|\s*\[\s*(\d+)\s*\])?', value.strip(), re.I)
    if not match:
        return None
    digits = match[2] or match[3] or '80'
    if len(digits) > 9:
        return None
    return match[1].upper(), int(digits)


def interface_type(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r'\s+', ' ', value.strip()).upper()
    if re.fullmatch(r'(?:POINTER TO )?[A-Z_]\w*', value, re.ASCII):
        return value
    value = re.sub(r'\[\s*(\d+)\s*\]$', r'(\1)', value)
    match = re.fullmatch(r'(POINTER TO )?(W?STRING)\s*\(\s*(\d+)\s*\)', value)
    if match and 0 < int(match[3]) <= 1_000_000:
        return (match[1] or '') + match[2] + '(' + str(int(match[3])) + ')'
    return None


def declared_return_type(declaration):
    # Stop before a variable block even if it is on the header line.
    header = re.split(r'\bVAR(?:_\w+)?\b', declaration, maxsplit=1, flags=re.I)[0]
    match = re.search(r'\b(?:FUNCTION|METHOD|PROPERTY)\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL|ABSTRACT)\s+)*\w+\s*:\s*([^\r\n;]+)', header, re.I)
    return interface_type(match[1]) if match else None
