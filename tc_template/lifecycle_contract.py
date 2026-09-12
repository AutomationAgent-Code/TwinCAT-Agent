"""Official lifecycle interface subset; no execution or online operations."""
import re


def lifecycle_findings(code):
    header = re.match(r'\s*METHOD\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL|ABSTRACT)\s+)*(FB_INIT|FB_REINIT|FB_EXIT)\b(?:\s*:\s*([^\r\n;]+))?', code, re.I)
    if not header:
        return []
    findings = []
    def add(pos, message):
        findings.append(('syntax-lifecycle-interface', pos, message))
    return_type = re.split(r'\bVAR\w*\b', header[2] or '', maxsplit=1, flags=re.I)[0].strip()
    if return_type.upper() != 'BOOL':
        add(header.start(), 'Lifecycle method return type must be BOOL.')
    inputs = {}
    for block in re.finditer(r'\b(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT)\b(.*?)\bEND_VAR\b', code, re.I | re.S):
        if block[1].upper() != 'VAR_INPUT':
            if block[2].strip():
                add(block.start(), 'Lifecycle methods must not add output or pass-through parameters.')
            continue
        for entry in re.finditer(r'\b(\w+)\s*:\s*(\w+)', block[2]):
            inputs[entry[1].upper()] = entry[2].upper()
    required = {'FB_INIT': ('BINITRETAINS','BINCOPYCODE'), 'FB_EXIT': ('BINCOPYCODE',), 'FB_REINIT': ()}[header[1].upper()]
    for name in required:
        if inputs.get(name) != 'BOOL':
            add(header.start(), 'Lifecycle interface requires VAR_INPUT ' + name + ': BOOL.')
    return findings
