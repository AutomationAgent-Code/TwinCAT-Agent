"""Online-first variable discovery and revalidated existing-mapping binding."""
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from . import _ps_bridge as ps
from .hmi_live import _select_one
from .hmi_binding import _find_tmc, _local, _children, _child, _text, _type_name
from tc_agent.dynamic_ads import browse_symbols, describe_symbol, DynamicAdsError


def _context(project, plc):
    info = ps.com_hmi_project_info(project)
    inventory = ps.ps_com('plc-runtimes')
    selected = _select_one(inventory.get('plcs') or [], plc, 'PLC runtime')
    port = selected.get('ads_port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Selected PLC ADS port is unresolved')
    endpoint = {'net_id': inventory.get('target_netid'), 'port': port, 'plc': selected['name']}
    ads = ps.com_hmi_ads_info(info['project_file'])
    runtimes = {r['name'] for r in ads.get('runtimes', []) if r.get('scope') == 'default'
                and r.get('enabled') and r.get('netid') == endpoint['net_id'] and r.get('port') == port}
    path = Path(info['project_file']).parent / 'Server/TcHmiSrv/TcHmiSrv.Config.default.json'
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    mappings = {}
    for key, item in config.get('SYMBOLS', {}).items():
        if not isinstance(item, dict) or item.get('DOMAIN') != 'ADS' or not item.get('USEMAPPING'):
            continue
        runtime, separator, symbol = str(item.get('MAPPING', '')).partition('::')
        if separator and runtime in runtimes:
            mappings.setdefault(symbol.replace('::', '.').casefold(), []).append({
                'server_symbol': key, 'page_expression': '%s%' + key + '%/s%',
                'runtime': runtime, 'access': item.get('ACCESS'), 'schema': item.get('SCHEMA'),
            })
    return info, endpoint, mappings


def _mapping_options(symbol, mappings):
    exact = mappings.get(symbol.casefold())
    if exact:
        return exact
    for root in sorted(mappings, key=len, reverse=True):
        if not symbol.casefold().startswith(root):
            continue
        suffix = symbol[len(root):]
        if not re.fullmatch(r'(?:\[\d+\]|\.[A-Za-z_][A-Za-z0-9_]*)+', suffix):
            continue
        # Root mapping plus HMI member tokens; the PLC path remains unchanged.
        return [{**m, 'mapped_parent': root,
                 'page_expression': '%s%' + m['server_symbol'] + suffix.replace('.', '::') + '%/s%'}
                for m in mappings[root]]
    return []


def search_variables(project='', plc='', query='', parent='', type_filter='', offset=0, limit=40,
                     source='auto'):
    if source not in {'auto', 'ads', 'tmc'}:
        raise ValueError('source must be auto, ads or tmc')
    info, endpoint, mappings = _context(project, plc)
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    failure = ''
    result = None
    if source != 'tmc':
        try:
            result = browse_symbols(endpoint['net_id'], endpoint['port'], query=query,
                                    parent=parent, type_filter=type_filter, offset=offset, limit=limit)
        except DynamicAdsError as exc:
            if source == 'ads':
                raise
            failure = str(exc)
    if result is None:
        if parent:
            return {'status': 'unavailable', 'source': 'tmc', 'online_error': failure,
                    'reason': 'Offline member expansion is unavailable; root TMC symbols only.',
                    'items': [], 'endpoint': endpoint, 'read_only': True}
        tmc = _find_tmc(info['solution'], endpoint['plc'])
        nodes = ET.parse(tmc).getroot()
        items = []
        for area in (n for n in nodes.iter() if _local(n.tag) == 'DataArea'):
            for symbol in _children(area, 'Symbol'):
                name, kind = _text(_child(symbol, 'Name')), _type_name(_child(symbol, 'BaseType'))
                if query.casefold() in name.casefold() and type_filter.casefold() in kind.casefold():
                    items.append({'path': name, 'type': kind})
        items.sort(key=lambda item: item['path'].casefold())
        result = {'source': 'tmc', 'items': items[offset:offset + limit], 'total': len(items),
                  'next_offset': offset + limit if offset + limit < len(items) else None,
                  'tmc_file': str(tmc), 'online_error': failure}
    for item in result['items']:
        matches = _mapping_options(item['path'], mappings)
        item.update(online_symbol_confirmed=result['source'] == 'ads-runtime',
                    value_read=False, mappings=matches,
                    binding_status='mapped' if len(matches) == 1 else ('ambiguous' if matches else 'mapping-required'))
        if len(matches) == 1 and result['source'] == 'ads-runtime':
            item['binding_candidate'] = {
                'project': info['project_file'], 'endpoint': endpoint, 'symbol': item['path'],
                'type': item['type'], 'mapping': matches[0],
            }
    return {**result, 'status': 'read', 'endpoint': endpoint, 'project_file': info['project_file'],
            'read_only': True, 'cache_hit': False,
            'note': 'Fresh symbol metadata per request; no stale cross-download cache. Unmapped roots use tc_hmi_bind_plc. Source-only declarations use plc_search/plc_read and are not online symbols.'}


def bind_candidate(candidate, file, control_id, attribute, apply=False):
    if not attribute.startswith('data-tchmi-') or attribute == 'data-tchmi-trigger':
        raise ValueError('Use an installed bindable control attribute; events use tc_hmi_control_events')
    info, endpoint, mappings = _context(candidate['project'], candidate['endpoint']['plc'])
    if endpoint != candidate['endpoint']:
        raise ValueError('PLC endpoint changed; search again before binding')
    current = _mapping_options(candidate['symbol'], mappings)
    if candidate['mapping'] not in current:
        raise ValueError('HMI mapping changed; search again before binding')
    parent = candidate['mapping'].get('mapped_parent')
    if parent and '[' in candidate['symbol'][len(parent):]:
        root_info = describe_symbol(endpoint['net_id'], endpoint['port'], parent)['symbol']
        dimensions = root_info.get('dimensions') or []
        if len(dimensions) != 1 or dimensions[0]['lower_bound'] != 0:
            raise ValueError('Nonzero/multidimensional/nested array mapping requires an explicit verified member mapping')
    # Re-resolve the exact online symbol after any possible PLC download.
    actual = describe_symbol(endpoint['net_id'], endpoint['port'], candidate['symbol'])
    if (actual.get('symbol') or {}).get('type') != candidate['type']:
        raise ValueError('PLC symbol type changed; search again before binding')
    result = ps.ps_com('hmi-control-edit', project=info['project_file'], file=file,
                       action='update', control_id=control_id,
                       attributes={attribute: candidate['mapping']['page_expression']}, apply=apply)
    return {**result, 'endpoint_verified': True, 'project_reloaded': False,
            'page_expression': candidate['mapping']['page_expression'],
            'hmi_server_values_verified': False}
