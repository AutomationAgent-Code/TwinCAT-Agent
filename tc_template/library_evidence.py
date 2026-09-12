"""Read exact resolved library versions, never infer signatures from names."""
from pathlib import Path
from xml.etree import ElementTree as ET


def inspect_reference(xml):
    root = ET.fromstring(xml)
    lib = root.find('./PlcLibDef/Library')
    if lib is None:
        return None
    result = {key: lib.findtext(tag, '') for key, tag in (
        ('name', 'LibraryName'), ('namespace', 'Namespace'), ('requested_version', 'Version'),
        ('effective_version', 'EffectiveVersion'), ('file', 'RelativePath'))}
    result.update(signature_verified=False, symbols=[], source='live_reference_xml')
    file = Path(result['file'])
    if not result['effective_version'] or not file.is_absolute() or not file.is_file():
        result['reason'] = 'Resolved library file/version unavailable'
        return result
    # Follow the exact live reference, never select the latest installed folder.
    cache = file.parent / 'browsercache'
    if not cache.is_file() or cache.stat().st_size > 8_000_000:
        result['reason'] = 'Bound library browser cache unavailable or exceeds budget'
        return result
    browser = ET.parse(cache).getroot()
    nodes = list(browser.iter('Node'))
    result['symbols'] = sorted({n.get('Name') for n in nodes if n.get('Name')})[:5000]
    result['symbols_truncated'] = len(nodes) > 5000
    result['reason'] = 'Symbol names only; full declaration/parameter signatures are not exposed by browsercache'
    return result
