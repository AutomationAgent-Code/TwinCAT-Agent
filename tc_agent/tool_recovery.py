"""Bounded, observable recovery of explicitly read-only infrastructure tools.

Never retries writes, builds, approvals, external MCP, or runtime operations.
The caller retains its host lease and permission checks on every invocation.
"""
from tc_agent.execution_policy import tool_succeeded

READ_RECOVERY_TOOLS = frozenset({'plc_diagnostics', 'plc_build_status'})


def transient_read(name, result):
    if name not in READ_RECOVERY_TOOLS or not isinstance(result, dict):
        return False
    if (tool_succeeded(result) or result.get('written') is True
            or result.get('uncertain') is True or result.get('status') == 'uncertain'
            or 'denied' in result or result.get('authorization_blocked')
            or result.get('status') in {'approval_expired', 'denied', 'cancelled'}):
        return False
    if name == 'plc_diagnostics':
        return result.get('diagnostics_complete') is False
    return result.get('status') == 'unavailable'


async def recover_read(name, invoke, progress):
    attempts = []
    for index in range(3):  # original read + at most two recovery reads
        result = await invoke()
        attempts.append(result)
        if not transient_read(name, result) or index == 2:
            break
        await progress(index + 1)
    if len(attempts) > 1 and isinstance(result, dict):
        result = {**result, 'recovery': {
            'attempts': len(attempts), 'retries': len(attempts) - 1,
            'recovered': tool_succeeded(result), 'history': attempts[:-1],
            'message': ('已恢复，只读重试完成。' if tool_succeeded(result) else
                        f'只读恢复 {len(attempts) - 1} 次后仍未完成；未自动修改程序或重新编译。'),
        }}
        if transient_read(name, result):
            available = result.get('diagnosticsAvailable', result.get('diagnostics_available')) is True
            result.update(recovery_exhausted=True,
                          recovery_reason=('diagnostic_evidence_incomplete' if available else 'read_service_unavailable'),
                          next_action=(
                              '诊断通道已返回数据，但错误分类或完整性证据仍不足。报告已取得的文件、行号及原始级别，检查诊断分类/计数；不要误报服务不可用，不要继续轮询或猜测改代码。'
                              if available else
                              '诊断/状态读取服务仍不可用。报告当前阻塞及日志，请用户检查 XAE 扩展或提供错误列表；不要继续轮询、改代码或重复编译。'))
    return result


async def recover_tool(name, invoke, diagnose, progress):
    """Recover build evidence, never re-execute the build or source mutation."""
    result = await recover_read(name, invoke, progress)
    build = result
    if name == 'plc_verify' and isinstance(result, dict):
        build = next((s.get('result') for s in result.get('stages', []) if s.get('stage') == 'build'), None)
    if (name in {'plc_build', 'plc_verify'} and isinstance(build, dict)
            and build.get('buildPerformed', build.get('build_performed')) is True
            and build.get('diagnostics_complete') is False
            and not build.get('uncertain') and build.get('status') != 'uncertain'):
        await progress(0)
        diagnostics = await recover_read('plc_diagnostics', diagnose, progress)
        recovered = isinstance(diagnostics, dict) and diagnostics.get('diagnostics_complete') is True
        result = {**result, 'diagnostic_recovery': diagnostics,
                  'recovery': {'recovered': recovered, 'history': [build],
                               'message': ('诊断已恢复，原构建仍未验证通过。' if recovered else
                                           '诊断恢复失败，未重新编译或修改程序。')},
                  'next_action': (diagnostics.get('next_action') if isinstance(diagnostics, dict) else
                                  '诊断服务不可用，请检查 XAE 扩展。')}
        if isinstance(diagnostics, dict) and diagnostics.get('recovery_exhausted'):
            result['recovery_exhausted'] = True
    return result


def recovery_dependencies(names):
    from tc_agent.tool_preconditions import is_source_mutation_tool
    if any(is_source_mutation_tool(name) or name in {'plc_build', 'plc_verify', 'plc_save_document'} for name in names):
        return {'plc_read', 'plc_find', 'plc_diagnostics', 'plc_build_status', 'tc_project_info'}
    return set()
