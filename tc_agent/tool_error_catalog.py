"""Stable error families. No classification depends on localized prose."""

# family -> level, impact, recovery instruction (not permission to execute)
FAMILIES = {
    'arguments': ('L2', 'step', '按当前工具 Schema 修正参数；不原样重试。'),
    'source': ('L2', 'step', '读取精确错误位置及当前源码，修正候选并重新预检。'),
    'conflict': ('L3', 'dependency', '重新读取目标和版本基线；不自动保存、丢弃或覆盖编辑。'),
    'identity': ('L3', 'dependency', '只读确认 XAE、工程及目标身份；不猜测或自动切换目标。'),
    'state': ('L3', 'dependency', '只读确认前置状态；所需状态变更另走正常审批。'),
    'authorization': ('L3', 'authorization', '申请或续期正式授权；不换工具绕过拒绝。'),
    'capability': ('L3', 'dependency', '补充契约或能力证据；暂停依赖步骤，可继续独立已授权工作。'),
    'diagnostics': ('L3', 'dependency', '有界恢复只读诊断；无完整证据不猜测修改或重复编译。'),
    'transport': ('L3', 'dependency', '只读检查连接；写入执行状态未知时先核对，禁止自动重放。'),
    'uncertain': ('L4', 'turn', '停止自动执行，只读核对已产生的影响；禁止自动重放写入。'),
    'cancelled': ('L3', 'turn', '尊重取消，不自动重启任务。'),
    'unknown': ('L2', 'step', '保留原始错误并检查日志；未知错误不得据此自动重试写入。'),
}

ERROR_TYPES = {
    'git_xae_path_conflict': 'conflict',
    'plc_editor_dirty': 'conflict',
    'source_editor_state_unknown': 'state',
    **dict.fromkeys(('tool_arguments', 'template_scope', 'hmi_dynamic_symbol_validation',
                     'hmi_event_validation', 'invalid_arguments', 'ValueError', 'KeyError', 'TypeError'), 'arguments'),
    **dict.fromkeys(('tool_precondition', 'hmi_binding_precondition', 'build_plan_required',
                     'plc_editor_online_conflict', 'document_save'), 'state'),
    **dict.fromkeys(('member_baseline', 'hmi_write_contract', 'build_state_conflict'), 'conflict'),
    'hmi_full_rewrite_not_acknowledged': 'authorization',
    'unknown_tool': 'capability', 'diagnostics_pending': 'diagnostics',
    'build_execution_uncertain': 'uncertain', 'xae_build_result_uncertain': 'uncertain',
    **dict.fromkeys(('TcComError', 'TimeoutError', 'ConnectionError', 'OSError',
                     'BrokenPipeError', 'ConnectionResetError'), 'transport'),
    'PermissionError': 'authorization', 'FileNotFoundError': 'identity',
    'RuntimeError': 'unknown', 'tool_exception': 'unknown',
}

CONDITIONS = {
    'git_repository': 'identity',
    **dict.fromkeys(('argument_schema', 'template_manifest', 'schema_gate', 'symbol_contract'), 'arguments'),
    **dict.fromkeys(('xae_identity', 'solution_scope', 'target_identity', 'runtime_selection',
                     'plc_object_scope', 'hmi_project_scope', 'configuration_scope',
                     'project_binding', 'source_scope', 'ads_endpoint', 'platform_mapping'), 'identity'),
    **dict.fromkeys(('source_conflict', 'source_editor_state', 'document_revision',
                     'member_tree_revision', 'expected_before', 'source_freshness'), 'conflict'),
    **dict.fromkeys(('build_mutex', 'editor_login_observation', 'mode_or_state',
                     'plc_editor_login', 'plc_runtime_run', 'transition_state'), 'state'),
    **dict.fromkeys(('tool_contract', 'tool_registry', 'editor_operation_capability'), 'capability'),
    'code_review_gate': 'source', 'approval': 'authorization', 'readback': 'diagnostics',
}

ERROR_CODES = dict.fromkeys((
    'build_platform_context_invalid', 'build_platform_mismatch', 'build_platform_unavailable',
    'platform_unavailable', 'platform_mismatch', 'platform_mapping_unavailable',
    'platform_mapping_mismatch', 'target_platform_unavailable', 'target_platform_mismatch',
), 'identity')
ERROR_CODES.update({
    'target_or_configuration_unavailable': 'identity',
    'explicit_platform_required': 'arguments',
    'configuration_change_requires_confirmation': 'authorization',
    'target_platform_confirmation_required': 'authorization',
    'context_changed': 'conflict',
})

FAILURE_STATUSES = {
    **dict.fromkeys(('invalid_arguments', 'invalid-manifest', 'invalid_tolerance', 'invalid_value'), 'arguments'),
    **dict.fromkeys(('conflict', 'stale', 'resync_required', 'configuration-mismatch'), 'conflict'),
    **dict.fromkeys(('approval_expired', 'authorization_plan_rejected', 'authorization_requested',
                     'confirmation_required', 'denied'), 'authorization'),
    **dict.fromkeys(('not_found', 'project_selection_required'), 'identity'),
    **dict.fromkeys(('unavailable', 'not_connected', 'disconnected'), 'transport'),
    **dict.fromkeys(('busy', 'precondition_failed', 'blocked', 'review_required'), 'state'),
    **dict.fromkeys(('unsupported',), 'capability'),
    **dict.fromkeys(('incomplete', 'verification_failed'), 'diagnostics'),
    **dict.fromkeys(('uncertain', 'write_result_unknown', 'written_readback_unavailable',
                     'written_verification_unavailable', 'created (code write failed)',
                     'created (impl write failed)'), 'uncertain'),
    'cancelled': 'cancelled', 'rolled_back': 'conflict',
    **dict.fromkeys(('error', 'failed', 'unknown'), 'unknown'),
}
