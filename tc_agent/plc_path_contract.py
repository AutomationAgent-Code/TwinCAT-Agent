"""Typed PLC identities at the tool boundary; never guess a disk-to-COM mapping."""

READS = {'plc_read', 'plc_read_fast', 'plc_read_smart'}
WRITES = {'plc_write', 'plc_patch', 'plc_delete', 'plc_delete_member',
          'plc_rename', 'plc_rename_member', 'plc_create_member', 'plc_create_property',
          'plc_save_document', 'plc_create', 'plc_create_folder', 'plc_create_standard_fb'}


def kind(value):
    if not value:
        return ''
    if value.upper().startswith('TIPC^'):
        if len(value.split('^')) < 4 or any(not p for p in value.split('^')):
            raise ValueError('tree_path 必须是完整 TIPC^… 对象路径')
        return 'tree'
    if '^' in value and value.lower().endswith(('.tcpou', '.tcdut', '.tcgvl', '.tcio')) and ':' not in value:
        return 'source'
    raise ValueError('PLC 路径类型不明确：tree_path 使用 TIPC^…；source_path 使用索引返回的项目^…文件路径，不使用物理文件路径')


def identity(args, *, write=False):
    result = dict(args)
    values = [(key, str(args.get(key) or '').strip()) for key in ('tree_path', 'source_path', 'path')]
    chosen, selected = '', ''
    for key, value in values:
        if not value:
            continue
        actual = kind(value)
        if key == 'tree_path' and actual != 'tree' or key == 'source_path' and actual != 'source':
            raise ValueError(key + ' 的路径类型错误；禁止在不同路径字段之间猜测转换')
        if chosen and (chosen != actual or selected.casefold() != value.casefold()):
            raise ValueError('PLC 路径字段冲突：只提交一个明确身份，不同时提交 COM 与索引路径')
        chosen, selected = actual, value
    if write and chosen == 'source':
        raise ValueError('写操作只能使用已核实的 tree_path；禁止用 source_path 或按同名对象转换')
    result.pop('tree_path', None)
    if chosen == 'tree':
        result['path'] = selected
        result.pop('source_path', None)
    elif chosen == 'source':
        result['source_path'] = selected
        result.pop('path', None)
    return result, chosen


def normalize(name, args):
    if name == 'plc_preflight':
        return {**args, 'candidates': [identity(c, write=True)[0] for c in args.get('candidates', [])]}
    if name not in READS | WRITES:
        return args
    if name == 'plc_read_fast':
        rows = [identity(r) for r in args.get('requests', [])]
        kinds = {k for _, k in rows if k}
        if len(kinds) > 1:
            raise ValueError('批量读取含 COM 与索引两种身份，请按 tree_path/source_path 分成两个批次；不会跨来源猜读')
        chosen = next(iter(kinds), '')
        result = {**args, 'requests': [r for r, _ in rows]}
    else:
        result, chosen = identity(args, write=name in WRITES)
    if name in READS and chosen:
        live = chosen == 'tree'
        if 'live' in args and bool(args['live']) != live:
            raise ValueError('live 与路径类型冲突：tree_path 为实时读取，source_path 为已保存索引读取')
        if name == 'plc_read' and not live:
            raise ValueError('plc_read 是实时读取；索引路径请使用 plc_read_smart 或 plc_read_fast')
        if name != 'plc_read':
            result['live'] = live
        if args.get('direct') and not live:
            raise ValueError('direct 不接受 source_path')
    return result


def extend_schema(tool):
    name = tool['name']
    if name not in READS | WRITES | {'plc_preflight'}:
        return
    schema = tool['parameters']
    if name == 'plc_read_fast':
        schema = schema['properties']['requests']['items']
    if name == 'plc_preflight':
        schema = schema['properties']['candidates']['items']
    props = schema['properties']
    props['tree_path'] = {'type': 'string', 'description': '首选：精确 TIPC^… COM 树路径；读取自动走实时 COM，写入不接受索引路径'}
    if 'path' in props:
        props['path'] = {**props['path'], 'description': '旧版兼容参数；优先使用 tree_path/source_path，类型不明确或与新字段冲突时拒绝'}
    if name in READS - {'plc_read'}:
        props['source_path'] = {'type': 'string', 'description': '已保存索引身份：原样使用目录的 source_path；绝不能填 TIPC^… 或物理文件路径'}
    if 'path' in schema.get('required', []):
        schema['required'].remove('path')
        schema.setdefault('allOf', []).append({'anyOf': [{'required': ['path']}, {'required': ['tree_path']}]})
    tool['description'] += ' 路径契约：tree_path=实时COM，source_path=磁盘索引；不混用。旧path仅严格兼容。'


def model_paths(value):
    """Keep legacy UI payloads intact; expose typed identities to the model."""
    if isinstance(value, list):
        return [model_paths(v) for v in value]
    if not isinstance(value, dict):
        return value
    # Opaque compare-and-save tokens must round-trip byte-for-field unchanged.
    result = {k: v if k in {'member_baseline', 'document_baseline', 'expected_member_baseline', 'expected_document_baseline'}
              else model_paths(v) for k, v in value.items()}
    source = result.get('source_path')
    if isinstance(source, str) and (':' in source or source.startswith(('\\\\', '/'))):
        result.setdefault('file', source)
        result.pop('source_path')
    path = result.get('path')
    if isinstance(path, str) and path:
        try:
            field = 'tree_path' if kind(path) == 'tree' else 'source_path'
        except ValueError:
            return result
        if not result.get(field) or result[field] == path:
            result[field] = path
            result.pop('path')
    return result
