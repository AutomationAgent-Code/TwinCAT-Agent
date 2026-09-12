"""Keep delivered PLC source verbatim across UI and prompt projections."""
SOURCE_READ_TOOLS = frozenset({'plc_read', 'plc_read_smart', 'plc_read_fast', 'plc_read_current'})


def contains_source(value):
    if not isinstance(value, dict):
        return False
    if any(isinstance(value.get(k), str) for k in ('declaration', 'implementation')):
        return True
    return any(contains_source(item) for key in ('methods', 'results')
               for item in (value.get(key) or []) if isinstance(item, dict))
