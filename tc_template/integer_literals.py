"""IEC integer literal subset, without evaluating source expressions."""
import re

WIDTHS = {'SINT': (8, True), 'USINT': (8, False), 'INT': (16, True),
          'UINT': (16, False), 'DINT': (32, True), 'UDINT': (32, False),
          'LINT': (64, True), 'ULINT': (64, False), 'BYTE': (8, False),
          'WORD': (16, False), 'DWORD': (32, False), 'LWORD': (64, False)}


def integer_literal(text):
    text = text.upper().replace('_', '')
    prefix = ''
    if '#' in text and text.split('#', 1)[0] in WIDTHS:
        prefix, text = text.split('#', 1)
    match = re.fullmatch(r'([+-]?)(?:(2|8|16)#)?([0-9A-F]+)', text)
    if not match or (not match[2] and not match[3].isdigit()):
        return None
    if len(match[3]) > 4096:
        return None
    try:
        value = int(match[3], int(match[2] or 10))
    except ValueError:
        return None
    return (-value if match[1] == '-' else value, prefix)


def fits_integer(value, typename):
    bits, signed = WIDTHS[typename]
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    return low <= value <= high
