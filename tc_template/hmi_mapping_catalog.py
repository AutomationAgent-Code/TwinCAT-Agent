"""Read saved static and dynamic mappings without ADS access or writes."""
import json
from pathlib import Path


def read_mappings(project='', runtime='', scope='', mapping_kind='both'):
    from . import _ps_bridge as ps
    if mapping_kind not in {'static', 'dynamic', 'both'}:
        raise ValueError('mapping_kind must be static, dynamic or both')
    result = ps.ps_com('hmi-ads-symbols', project=project, runtime=runtime, scope=scope) if mapping_kind != 'dynamic' else {}
    result = {**result, 'readonly': True, 'mapping_kind': mapping_kind,
              'static_symbols': result.get('symbols', []), 'dynamic_symbols': [],
              'dynamic_available': False, 'online_verified': False}
    if mapping_kind == 'static':
        return result
    info = ps.com_hmi_project_info(project)
    result['project_file'] = info['project_file']
    if scope == 'remote':
        result['dynamic_note'] = 'Remote dynamic configuration is not inspected; do not infer missing mappings.'
        return result
    path = Path(info['project_file']).parent / 'Server/TcHmiSrv/TcHmiSrv.Config.default.json'
    if not path.is_file():
        result['dynamic_note'] = 'Saved default Server configuration unavailable; not proof of missing mappings.'
        return result
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    for key, item in data.get('SYMBOLS', {}).items():
        if not isinstance(item, dict) or item.get('DOMAIN') != 'ADS' or item.get('DYNAMIC') is not True:
            continue
        if runtime and not str(item.get('MAPPING', '')).startswith(runtime + '::'):
            continue
        result['dynamic_symbols'].append({'server_symbol': key, 'page_expression': '%s%' + key + '%/s%',
            'mapping': item.get('MAPPING'), 'schema': item.get('SCHEMA'), 'scope': 'default'})
    result.update(dynamic_available=True, dynamic_symbol_count=len(result['dynamic_symbols']),
                  note='Static and dynamic mappings are separate; symbol_count is legacy static count. No PLC access performed.')
    return result
