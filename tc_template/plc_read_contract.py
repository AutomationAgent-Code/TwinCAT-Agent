"""Read diagnostics only: never reload projects or guess COM paths."""


class PlcReadError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'[{code}] {message}')


def validate_object_request(name, path=''):
    if str(name).strip().lower().endswith(('.plcproj', '.tsproj', '.sln')):
        raise PlcReadError('project_not_code_object',
                           '工程文件不是 POU/DUT/GVL/接口。工程信息用 tc_project_info；'
                           '代码先用 plc_find/plc_tree 定位对象，不要更换路径重复读取工程文件。')


def diagnose_read_failure(name, path, error, inventory):
    """Only classify evidence we have; inaccessible does not prove Disabled."""
    plcs = inventory.get('plcs', [])
    selected = [p for p in plcs if path.lower().startswith('tipc^' + str(p.get('name', '')).lower() + '^')]
    project_match = [p for p in plcs if str(name).casefold() in
                     {str(p.get('name', '')).casefold(), str(p.get('project_name', '')).casefold()}]
    if project_match and not (path.lower().startswith('tipc^') and path.count('^') >= 4):
        return PlcReadError('project_not_code_object',
                            '传入的是 PLC 项目名，不是代码对象。工程信息用 tc_project_info；'
                            '用 plc_find/plc_tree 返回的对象名和精确路径读取代码。')
    scope = selected or plcs
    if not scope or any(not p.get('project_name') for p in scope):
        return PlcReadError('plc_project_unavailable',
                            'PLC NestedProject 不可用或未发现 PLC 项目；可能尚未加载、禁用或加载失败，'
                            '当前证据不能确认 Disabled。请检查 XAE 加载状态，不要自动重载。原始错误: ' + str(error))
    message = str(error)
    if 'outside the current PLC project' in message or 'does not resolve' in message:
        return PlcReadError('invalid_object_path',
                            '不是当前 PLC 的有效代码对象路径；请原样使用 plc_find 的精确 COM path。原始错误: ' + message)
    if 'not found' in message.lower():
        return PlcReadError('code_object_not_found',
                            'PLC 项目可访问，但未找到指定对象或成员；请用 plc_find 确认名称和路径。原始错误: ' + message)
    return error
