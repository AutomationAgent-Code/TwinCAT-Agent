"""Declarative precondition contracts shared by Agent tool frontends.

The contracts deliberately describe independent state dimensions.  An XAE
process being attached, a PLC project being logged in, the PLC runtime being
in Run, and a source document being saved are different facts and must not be
collapsed into one ``online`` flag.
"""
from __future__ import annotations


def _condition(name: str, expected: str, check: str, *, volatile: bool = False) -> dict:
    return {
        "condition": name,
        "expected": expected,
        "check": check,
        "volatile": volatile,
    }


def _source_mutation() -> list[dict]:
    return [
        _condition(
            "xae_identity",
            "the bound XAE host is attached and still has the selected solution",
            "connect-check; compare the returned PID and solution with the bound scope",
        ),
        _condition(
            "plc_object_scope",
            "one exact PLC object/member and its parent tree path",
            "resolve the object through the current PLC tree before COM mutation",
        ),
        _condition(
            "source_conflict",
            "the target source revision has not changed since the candidate was read",
            "compare expected_source_hash(es) when supplied; patch also requires one unique old_text",
            volatile=True,
        ),
        _condition(
            "source_editor_state",
            "PLC editor is logged out; unsaved edits are included in the live baseline or explicitly resolved",
            "inspect the target document's saved/revision state; do not save or discard automatically",
            volatile=True,
        ),
        _condition(
            "code_review_gate",
            "the current write candidate passes the existing PLC review and dependency checks",
            "review immediately before COM mutation",
            volatile=True,
        ),
        _condition(
            "approval",
            "the outer Agent permission policy has allowed this non-readonly action",
            "permission mode/plan is checked before dispatch",
        ),
    ]


_SOURCE_MUTATIONS = {
    "plc_write", "plc_patch", "plc_create", "plc_create_folder",
    "plc_create_member", "plc_create_property", "plc_delete",
    "plc_delete_member", "plc_rename", "plc_rename_member",
    "plc_restore_snapshot", "plc_create_project", "plc_remove_project",
    "plc_delete_project", "plc_import_plcopen", "plc_lib_add",
    "plc_lib_remove", "plc_placeholder_add", "plc_lib_install",
    "plc_coding_profile_set", "fblib_add", "plc_create_standard_fb",
    "plc_git_sync",
}

_ADS_READS = {"plc_read_value", "plc_read_values", "tc_task_runtime_info"}
_ADS_WRITES = {"plc_write_value", "plc_write_values"}
_RUNTIME_COMMANDS = {"tc_login", "tc_logout", "tc_start", "tc_stop", "tc_online"}

_HMI_MUTATIONS = {
    name for name in (
        "tc_hmi_write_markup", "tc_hmi_item_create", "tc_hmi_item_delete",
        "tc_hmi_create_view", "tc_hmi_project_api", "tc_hmi_startup_view_set",
        "tc_hmi_control_events", "tc_hmi_control_edit", "tc_hmi_controls_batch",
        "tc_hmi_delete_view", "tc_hmi_ads_runtime_set", "tc_hmi_ads_symbol_set",
        "tc_hmi_dynamic_symbols_set", "tc_hmi_bind_variable", "tc_hmi_bind_plc",
        "tc_hmi_internal_symbol_set", "tc_hmi_localization_set",
        "tc_hmi_themed_resource_set", "tc_hmi_active_theme_set",
        "tc_hmi_user_control_create", "tc_hmi_user_control_parameter_set",
        "tc_hmi_user_control_delete", "tc_hmi_framework_attribute_set",
        "tc_hmi_framework_event_set", "tc_hmi_framework_create",
        "tc_hmi_framework_pack", "tc_hmi_framework_install",
        "tc_hmi_framework_uninstall", "tc_hmi_server_control",
        "tc_hmi_create_project",
    )
}

_SYSTEM_MUTATIONS = {
    "tc_activate", "tc_restart", "tc_config_mode", "tc_run_mode",
    "tc_system_settings_set", "tc_realtime_settings_set", "tc_core_assign",
    "tc_task_core_assign", "tc_task_settings_set", "tc_system_add",
    "tc_system_remove", "tc_io_create", "tc_io_remove", "tc_io_remove_master",
    "tc_scan_devices", "nc_axis_params_set", "nc_axis_move",
    "nc_create_task", "nc_create_axis", "nc_link_drive", "nc_quick_link",
    "nc_link_encoder",
}

_HMI_READS = {
    "tc_hmi_project_info", "tc_hmi_structure", "tc_hmi_source_index",
    "tc_hmi_source_catalog", "tc_hmi_read_smart", "tc_hmi_read",
    "tc_hmi_ads_info", "tc_hmi_validate", "tc_hmi_control_schema",
    "tc_hmi_ads_symbols", "tc_hmi_bindings", "tc_hmi_variable_search",
    "tc_hmi_internal_symbols", "tc_hmi_localizations", "tc_hmi_themes",
    "tc_hmi_user_controls", "tc_hmi_framework_templates",
    "tc_hmi_framework_validate", "tc_hmi_framework_control_info",
    "tc_hmi_framework_packages", "tc_hmi_framework_package_inspect",
    "tc_hmi_runtime_info", "tc_hmi_ads_live_check", "tc_hmi_binding_diagnose",
    "tc_hmi_browser_validate", "tc_hmi_diagnostics", "tc_hmi_build",
}

_PLC_READS = {
    "plc_list", "plc_find", "plc_structure", "plc_tree", "plc_read",
    "plc_read_fast", "plc_read_smart", "plc_source_index", "plc_source_catalog",
    "plc_read_current", "plc_dirty_current", "plc_build_status", "plc_build", "plc_diagnostics",
    "plc_preflight", "plc_verify", "plc_libraries", "plc_review", "plc_vars",
    "plc_search", "plc_static_constraints", "plc_static_analysis",
    "plc_coding_profile",
}
_BUILD_TOOLS = {"plc_build", "plc_diagnostics", "plc_preflight", "plc_verify"}
_BUILD_EXECUTION_TOOLS = {"plc_build", "plc_verify"}


def is_source_mutation_tool(name: str) -> bool:
    """Return whether a registered tool changes PLC/source project state."""
    return name in _SOURCE_MUTATIONS


def contract_for_tool(name: str, category: str = "", readonly: bool = True) -> list[dict]:
    """Return the declared conditions for one registered tool.

    This is metadata only; execution uses the same profile to run the
    lightweight checks that are safe to perform before dispatch.  Profiles
    intentionally do not infer conditions from request wording.
    """
    if name == 'fblib_add':
        return [_condition('xae_identity', 'bound XAE and selected solution', 'connect-check'),
                _condition('template_manifest', 'slug resolves to a local rendered template with valid object names',
                           'render_fb(slug,params) before import; do not require a nonexistent name/pou argument'),
                _condition('approval', 'normal per-call write approval', 'Agent permission gate; no fallback to unreviewed handwritten code')]
    if name == 'plc_save_document':
        return [_condition('xae_identity', 'exact bound XAE and solution', 'strict PID native bridge'),
                _condition('document_revision', 'exact open parent document and unchanged live/disk baseline',
                           'plc_read(document_baseline=true); compare immediately before Document.Save; dirty is permitted', volatile=True),
                _condition('approval', 'explicit user save request and per-call approval even in auto/accept',
                           'danger=save_document; never SaveAll/close/reopen; no automatic retries')]
    if name in _SOURCE_MUTATIONS:
        conditions = _source_mutation()
        if name in {'plc_delete_member', 'plc_rename_member'}:
            conditions.append(_condition(
                'member_tree_revision',
                'dirty source requires an exact live parent/member tree baseline; saved state alone is not a source revision',
                'plc_read(structure_baseline=true) -> expected_member_baseline; native same-session compare before mutation and full tree readback; carry the new returned member_baseline to the next edit',
                volatile=True))
        if name in {"plc_create_member", "plc_create_property", "plc_delete_member", "plc_rename_member"}:
            conditions.append(_condition(
                "editor_operation_capability",
                "the specific structure operation is permitted by XAE; PLC Run is not an editor Login flag",
                "plc_editor_state for the exact runtime and actual XAE rejection; Logout only with separate approval if required",
                volatile=True))
        return conditions
    if name == "plc_editor_state":
        return [_condition("editor_login_observation",
            "one selected PLC project's Login/Logout state; unknown remains unknown, no Runtime Run inference",
            "plc-online-state project OnlineSettings; multi-PLC requires an exact runtime", volatile=True)]
    if name in _ADS_WRITES:
        return [
            _condition("target_identity", "one current target AMS NetId", "target-show"),
            _condition("ads_endpoint", "one exact runtime and actual ADS port", "plc-runtimes; no port guessing", volatile=True),
            _condition("plc_runtime_run", "ADS PLC state Run", "read the selected PLC ADS state immediately before write", volatile=True),
            _condition("expected_before", "the optional expected_before value matches the live value", "read symbol before write", volatile=True),
            _condition("readback", "the write result is read back and compared", "write tool readback contract", volatile=True),
            _condition("approval", "outer Agent permission policy allows the runtime write", "permission mode/plan before dispatch"),
        ]
    if name in _ADS_READS:
        return [
            _condition("target_identity", "one current target AMS NetId", "target-show"),
            _condition("ads_endpoint", "one exact runtime and actual ADS port", "plc-runtimes; no port guessing", volatile=True),
            _condition("symbol_contract", "symbol name/type can be resolved without raw address guessing", "dynamic symbol or typed symbol read", volatile=True),
        ]
    if name in _RUNTIME_COMMANDS:
        return [
            _condition("xae_identity", "the bound XAE host and solution remain unchanged", "project-info/connect-check before transition", volatile=True),
            _condition("target_identity", "the selected target AMS NetId matches the live project", "project-info and platform target evidence", volatile=True),
            _condition("runtime_selection", "one exact PLC runtime, or explicit all_plcs=true", "plc-runtimes; actual ADS port", volatile=True),
            _condition("platform_mapping", "active local platform/mapping matches the target when required", "platform-show and platform-target-info"),
            _condition("transition_state", {
                "tc_login": "selected XAE project login/download state is checked; Login does not authorize Start",
                "tc_logout": "selected XAE project login state is checked; Logout is not PLC Stop or Config",
                "tc_start": "selected PLC runtime is ready to start; already Run does not need another Start",
                "tc_stop": "selected PLC runtime stop is explicitly requested; editing does not authorize Stop",
                "tc_online": "selected project Login/load is verified before separately scoped Start; partial results remain explicit",
            }[name], "OnlineSettings and actual ADS state appropriate to this command; do not conflate them", volatile=True),
            _condition("approval", "outer Agent permission policy allows this runtime transition", "permission mode/plan before dispatch"),
        ]
    if name in _HMI_MUTATIONS:
        return [
            _condition("xae_identity", "the bound XAE host and selected solution remain unchanged", "connect-check"),
            _condition("hmi_project_scope", "one exact registered HMI project/file/control", "project registration and control schema"),
            _condition("source_conflict", "the target HMI source hash is unchanged and no target editor buffer is dirty", "write contract source hash/unsaved checks", volatile=True),
            _condition("schema_gate", "the installed Framework schema accepts the requested change", "control/schema validation immediately before write", volatile=True),
            _condition("approval", "outer Agent permission policy allows this HMI mutation", "permission mode/plan before dispatch"),
        ]
    if name in _SYSTEM_MUTATIONS:
        return [
            _condition("xae_identity", "the bound XAE host and solution remain unchanged", "connect-check/project-info", volatile=True),
            _condition("target_identity", "one exact target is selected and still matches", "target-show/platform target evidence", volatile=True),
            _condition("configuration_scope", "the exact SYSTEM/I/O/NC tree path is resolved", "structure/readback before mutation"),
            _condition("mode_or_state", "the tool-specific Config/Run/ADS state is satisfied", "read actual mode immediately before action", volatile=True),
            _condition("approval", "outer Agent permission policy allows this mutation", "permission mode/plan before dispatch"),
        ]
    if name in _HMI_READS:
        return [
            _condition("xae_identity", "a bound XAE host and selected solution are available", "connect-check"),
            _condition("hmi_project_scope", "one exact registered HMI project/file when applicable", "HMI project resolver"),
            _condition("source_freshness", "saved-source results are marked non-live; online results identify their endpoint", "source/index/live result metadata", volatile=True),
        ]
    if name in _BUILD_TOOLS:
        return [
            _condition("xae_identity", "the bound XAE host and selected solution remain unchanged", "connect-check", volatile=True),
            _condition("project_binding", "the requested PLC project is bound to the selected solution", "project-info and PLC project resolution"),
            _condition("build_mutex", "no competing build/diagnostics operation owns the project", "existing build guard immediately before build", volatile=True),
            _condition("source_freshness", "diagnostics identify whether they describe this build or an older result", "build result diagnostics provenance", volatile=True),
            *([_condition("approval", "the exact Build/Rebuild action and fresh state token are user-approved", "plc_build_status token plus outer permission gate", volatile=True)]
              if name in _BUILD_EXECUTION_TOOLS else []),
        ]
    if name in _PLC_READS:
        return [
            _condition("source_scope", "the requested PLC solution/object is resolved", "solution binding and exact PLC tree path when applicable"),
            _condition("source_freshness", "live XAE reads are distinguished from saved/index/cache reads", "source/live_xae/dirty_unknown result fields", volatile=True),
        ]
    if name.startswith(("tc_", "nc_")) and name not in {"tc_connect_check", "tc_solution_templates"}:
        return [_condition("xae_identity", "the requested XAE/System Manager scope is available", "connect-check and exact target/project resolution", volatile=True)]
    if category in {"项目", "库", "导入导出", "版本", "代码生成"} and not readonly:
        return [_condition("xae_identity", "the selected solution/project scope is available", "connect-check/project resolution")]
    return []


def precondition_failure(*, status: str, condition: str, expected: object,
                         actual: object, scope: object, reason: str,
                         next_action: str, failed_preconditions: list[dict] | None = None) -> dict:
    """Build the stable machine-readable failure shape used by run_tool."""
    item = {
        "condition": condition,
        "expected": expected,
        "actual": actual,
        "scope": scope,
        "reason": reason,
        "next_action": next_action,
        "not_executed": True,
    }
    return {
        "status": status,
        "error_type": "tool_precondition",
        "error": reason,
        "retry_safe": False,
        **item,
        "failed_preconditions": failed_preconditions or [item],
    }


def editor_login_failure(error: object) -> dict | None:
    """Interpret only an explicit XAE login rejection, never ADS Run/Stop."""
    if "cannot add an object because it affects a device you are currently logged into" not in str(error).casefold():
        return None
    return {
        "status": "conflict", "error_type": "plc_editor_online_conflict",
        "condition": "plc_editor_login", "error": str(error), "retry_safe": False,
        "reason": "XAE 因工程已登录而拒绝结构操作；这不是 PLC Runtime 的 Run/Stop 限制。",
        "next_action": "先回读目标对象确认是否有部分变更，再用 plc_editor_state 读取对应工程登录状态。若需离线结构修改，单独申请 tc_logout 审批；不要 Stop、切 Config 或重启。",
        "runtime_state": "not_queried",
    }
