"""
tc_agent.agent_core — 自研 agent 循环 + Provider 适配层 (P1-A)

不依赖 claude-agent-sdk / claude CLI / 任何个人登录。纯 HTTP 直调各家模型 API。
循环本身与厂商无关:它只操作一个**中立会话模型**,由 Provider 适配器负责翻译
成各家线格式(wire format)。工具直接调 tc_template 的 COM 桥(去 MCP 中间层)。

中立会话模型
------------
system: str
history: list[dict]，每条是下列之一：
    {"role":"user",      "text": str}
    {"role":"assistant", "text": str, "tool_calls":[{"id","name","args":dict}]}
    {"role":"tool",      "id": str, "name": str, "result": <可 JSON 序列化>}
tools: list[dict]  ——  [{"name","description","parameters": <JSON schema>}]

Provider.complete(system, history, tools) -> AssistantStep:
    {"text": str, "tool_calls":[{"id","name","args":dict}], "usage":{"in":int,"out":int}}

三种协议
--------
- OpenAIProvider   : chat/completions + function calling(DeepSeek/Kimi/GLM/Qwen/OpenAI)
- ResponsesProvider: OpenAI Responses API（Codex，含加密推理状态回放）
- AnthropicProvider: Messages API + tool use(Claude；DeepSeek 也提供兼容端点)

跑法(DeepSeek 两个端点都能测)：
    py -3.14 -m tc_agent.agent_core "当前项目里有哪些 PLC 对象？"
    py -3.14 -m tc_agent.agent_core --proto anthropic "读一下 MAIN 并解释"
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tc_template._ps_bridge import com_select_build_platform
from tc_template._ps_bridge import (  # noqa: E402
    ps_com, ps_io_configuration, com_build, com_diagnostics, tool_target, TcComError,
    list_variables, search_code_result, find_pou,
)
from tc_template.lint import review_write_candidate  # noqa: E402
from tc_template.hmi_events import control_events, ACTION_SCHEMA, validate_hmi_file  # noqa: E402
from tc_template.hmi_binding import bind_plc as bind_hmi_plc  # noqa: E402
from tc_template.hmi_live import check_hmi_ads_online  # noqa: E402
from tc_template.hmi_diagnostics import read_hmi_diagnostics  # noqa: E402
from tc_template import hmi_source  # noqa: E402
from tc_template.hmi_contract import control_schema as hmi_control_schema  # noqa: E402
from tc_template._ps_bridge import (  # noqa: E402
    com_hmi_binding_diagnose, com_hmi_browser_validate, com_hmi_build,
)
from tc_template.static_analysis import analyze_objects  # noqa: E402
from tc_template.plc_constraints import constraints as plc_static_constraints  # noqa: E402
from tc_template.plc_syntax import syntax_templates  # noqa: E402
from tc_template.plc_build_diagnostics import build_diagnostics  # noqa: E402
from tc_agent.build_execution import (  # noqa: E402
    capture_build_state, execute_plc_build,
)
from tc_template.tc_platform import (  # noqa: E402
    list_static_routes, resolve_target, validate_realtime_snapshot,
)
from tc_agent import config as cfgmod  # noqa: E402
from tc_agent import coding_profile  # noqa: E402
from tc_agent.conversation_store import (  # noqa: E402
    active_conversation_snapshot,
    tool_memory_forget, tool_memory_list, tool_memory_remember,
    tool_project_data_backup, tool_project_data_export, tool_project_data_health,
    tool_thread_fork, tool_thread_inbox, tool_thread_list, tool_thread_rename,
    tool_thread_send,
)
from tc_agent import docsearch  # noqa: E402
from tc_agent import plc_versions  # noqa: E402
from tc_agent import reporting  # noqa: E402
from tc_agent.execution_policy import tool_succeeded, gate_rejected, blocked_batch_result
from tc_agent.plc_cache import CACHE as PLC_SOURCE_CACHE, scoped_solution  # noqa: E402
from tc_agent.plc_source import read_source, read_source_file  # noqa: E402
from tc_agent.plc_source_index import (  # noqa: E402
    read as read_indexed_source, read_many as read_many_indexed_source,
    status as source_index_status, search as search_indexed_source,
    catalog as source_index_catalog,
)
from tc_agent.dynamic_ads import (  # noqa: E402
    DynamicAdsError, read_symbol as read_dynamic_ads_symbol,
    write_symbol as write_dynamic_ads_symbol,
)
from tc_agent.ads import (  # noqa: E402
    ADS_SYSTEM_SERVICE_PORT, ADSSTATE_RECONFIG, ADSSTATE_RESET, AdsStateError,
    read_ads_state, read_ads_values_by_name, write_ads_control,
    write_ads_values_by_name,
)
from tc_agent.plc_value_validation import (  # noqa: E402
    ValueNormalizationError, canonical_kind, normalize_scalar,
    normalize_tolerance, scalar_values_match, is_known_scalar_kind,
)
from tc_agent.system_manager_interface import (  # noqa: E402
    DirectSystemManager, EndpointUnavailable, ProtocolError,
    SystemManagerWebSocketTransport, discover_system_manager_endpoint,
)
from tc_agent.system_manager_diagnostics import normalize_system_manager_diagnostics  # noqa: E402
from tc_agent.system_manager_batch import BatchExecutor, BatchItem  # noqa: E402
from tc_agent.tool_preconditions import (  # noqa: E402
    contract_for_tool, is_source_mutation_tool, precondition_failure, editor_login_failure,
)

from tc_agent.tool_usage_contract import prompt_contract as tool_usage_prompt
from tc_agent.tool_contracts import compile_contract, schema_contract_note, selected_contract_prompt

SYSTEM_PROMPT = (
    "你是 TwinCAT Agent，嵌在 TwinCAT XAE 旁边，帮助 TwinCAT 3 自动化开发。"
    "需要了解 XAE 当前状态或 PLC 代码时，调用提供的工具，不要凭空猜测。"
    "回答用中文，简洁。"
    "只读/查看/诊断请求不得自行扩展为切平台、登录、启动或部署。"
    "tc_login 返回 ProgramLoaded 且 application_state=Run 时不要继续调用 tc_start；"
    "需要刷新时使用只读状态工具。already_satisfied 表示原本已满足、未发送命令，"
    "必须表述为‘已运行，跳过启动’，不能说‘执行启动成功’。"
    "工具前置条件必须分别核对 XAE 宿主/解决方案、PLC 编辑器 Login/在线编辑语境、"
    "PLC Runtime ADS 状态、源保存与 revision、目标/端口和权限；PLC Run 不等于编辑器在线，"
    "未知状态返回结构化 unknown，不自动保存/丢弃/Logout/Stop/Config/Start/Restart。"
) + tool_usage_prompt()

def _saved_solution() -> str:
    # The backend binds the exact solution for this task. Standalone calls
    # resolve through their PID-bound bridge; never share a process-global path.
    return scoped_solution() or str(ps_com("project-info").get("solution") or "")

# ======================================================================
#  工具注册表:直接调 tc_template 的 COM 桥(去 MCP 中间层)
#
#  每个工具 = {name, description, parameters(JSON schema), readonly, run(args)->obj,
#              category, danger, protocol_version, side_effect, idempotency}
#  readonly=True 的工具将来由权限门自动放行;False 的(改代码/改运行时)需审批。
#  category 仅用于分组展示。danger 非空表示即使在“编辑放行”模式下也必须确认。
# ======================================================================
def _tool(name, desc, params, readonly, run, category="", danger="", *,
          preconditions=None):
    params = {"additionalProperties": False, **params}
    return {"name": name, "description": desc, "parameters": params,
            "readonly": readonly, "category": category, "danger": danger, "run": run,
            "protocol_version": 1,
            "side_effect": "none" if readonly else (danger or "project"),
            "idempotency": "safe_replay" if readonly else "ledger_guarded",
            "preconditions": list(preconditions or [])}


_MEMBER_BASELINE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': '原样使用 plc_read(structure_baseline=true) 返回的 member_baseline；删除/重命名成功后使用新的回读基线。',
    'properties': {'version': {'type': 'integer', 'enum': [1]},
                   'path': {'type': 'string'}, 'solution': {'type': 'string'},
                   'window': {'type': 'integer'},
                   'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
    'required': ['version', 'path', 'solution', 'window', 'sha256'],
}


def _tool_precondition_failure(
    name: str, args: dict, prefer_pid: int, tool: dict,
) -> dict | None:
    """Run the lightweight, read-only checks declared by one tool.

    The actual tool remains responsible for its final live verification (for
    example ADS readback or HMI schema checks).  This layer catches scope and
    identity mistakes before dispatch and never changes XAE/PLC state.
    """
    conditions = list((tool.get("contract") or {}).get("preconditions") or [])
    if not conditions:
        return None
    names = {str(item.get("condition") or "") for item in conditions}
    source_mutation = is_source_mutation_tool(name)
    explicit_solution = bool(str(args.get("solution") or "").strip())
    cached_solution = bool(str(scoped_solution() or "").strip())
    offline_source = (
        (
            name in {"plc_source_index", "plc_source_catalog"}
            and explicit_solution
        )
        or (
            name in {"plc_read_fast", "plc_read_smart", "plc_search"}
            and not bool(args.get("live", False))
            and (explicit_solution or cached_solution)
        )
    )
    # Saved HMI index reads are intentionally usable without a live XAE
    # connection. A preview is also non-mutating; the concrete HMI contract
    # still validates the request when it is dispatched.
    offline_hmi = (
        name in {"tc_hmi_source_index", "tc_hmi_source_catalog", "tc_hmi_read_smart"}
        and bool(str(args.get("project") or "").strip())
    )
    hmi_preview = (
        name.startswith("tc_hmi_")
        and "apply" in args
        and args.get("apply") is False
    )
    context = None

    def get_context():
        nonlocal context
        if context is not None:
            return context
        try:
            context = ps_com("connect-check")
        except Exception as exc:  # noqa: BLE001
            return exc
        return context

    # HMI writes already have a stronger, tool-specific guarded contract
    # (source hash/dirty state/schema/project reload). Keep this shared layer
    # declarative for them, but do not duplicate a weak generic connect-check
    # that would turn a valid handler result into an opaque precondition error.
    hmi_guarded_mutation = "schema_gate" in names
    needs_context = bool(names & {
        "xae_identity", "plc_object_scope", "source_editor_state",
        "hmi_project_scope", "source_scope",
    }) and not (offline_source or offline_hmi or hmi_preview or hmi_guarded_mutation)
    if needs_context:
        current = get_context()
        if isinstance(current, Exception):
            return precondition_failure(
                status="unavailable", condition="xae_identity",
                expected="a reachable XAE Automation Interface host",
                actual={"error": str(current)},
                scope={"tool": name, "preferred_pid": prefer_pid},
                reason="无法读取当前 XAE 宿主身份，不能安全执行依赖 XAE 的工具。",
                next_action="确认目标 XAE 仍在运行并重新读取 tc_connect_check；不要自动启动或新建 XAE。",
            )
        if not isinstance(current, dict):
            return precondition_failure(
                status="unknown", condition="xae_identity",
                expected="structured XAE identity with PID and solution",
                actual={"value_type": type(current).__name__},
                scope={"tool": name, "preferred_pid": prefer_pid},
                reason="XAE 身份返回不是可核对的结构化数据。",
                next_action="先重新执行 tc_connect_check，确认宿主和解决方案后再重试。",
            )
        actual_pid = int(current.get("pid") or 0)
        if prefer_pid and not actual_pid:
            return precondition_failure(
                status="unknown", condition="xae_identity",
                expected={"pid": prefer_pid}, actual=current,
                scope={"tool": name, "preferred_pid": prefer_pid},
                reason="无法确认本次工具绑定的 XAE PID，存在多宿主误操作风险。",
                next_action="在目标 XAE 面板中重新绑定后再执行；不要猜测或切换到其它 XAE。",
            )
        if prefer_pid and actual_pid and actual_pid != int(prefer_pid):
            return precondition_failure(
                status="conflict", condition="xae_identity",
                expected={"pid": int(prefer_pid)}, actual={"pid": actual_pid},
                scope={"tool": name, "preferred_pid": int(prefer_pid)},
                reason="当前 COM 连接的 XAE PID 与调用方绑定的宿主不一致。",
                next_action="回到对应 XAE 面板重新绑定；不跨 XAE 重试写操作。",
            )
        if source_mutation or "source_scope" in names or "hmi_project_scope" in names:
            solution = str(current.get("solution") or "").strip()
            if not solution:
                return precondition_failure(
                    status="blocked", condition="solution_scope",
                    expected="one open, identifiable solution",
                    actual={"solution": "", "solution_open": current.get("solution_open")},
                    scope={"tool": name, "preferred_pid": prefer_pid},
                    reason="当前 XAE 没有可确认的解决方案，拒绝将操作发到模糊工程范围。",
                    next_action="先打开或创建目标解决方案，再执行项目/PLC/HMI工具；不自动调用 tc_open。",
                )

        if source_mutation or "source_editor_state" in names:
            active = current.get("active_document") or {}
            full_name = str(active.get("full_name") or active.get("source_file") or "")
            target = str(args.get("pou") or args.get("name") or "").strip().casefold()
            file_stem = full_name.rsplit("\\", 1)[-1].split("/", 1)[-1]
            file_stem = file_stem.split("@", 1)[0].rsplit(".", 1)[0].casefold()
            if target and file_stem and target == file_stem:
                if "saved" not in active or active.get("saved") is None:
                    return precondition_failure(
                        status="unknown", condition="source_editor_state",
                        expected={"saved": True, "target": target},
                        actual={"active_document": active},
                        scope={"tool": name, "object": target, "document": full_name},
                        reason="目标 PLC 文档已定位，但 XAE 没有返回可核对的保存状态。",
                        next_action="在 XAE 中重新读取当前文档状态；不要把未知状态当作已保存或离线。",
                    )
                baseline = args.get("expected_source_hashes")
                guarded_live_edit = (name in {"plc_write", "plc_patch"}
                                     and bool(args.get("path"))
                                     and isinstance(baseline, dict)
                                     and set(baseline) == {"declaration", "implementation"}
                                     and all(re.fullmatch(r"[0-9a-fA-F]{64}", str(v)) for v in baseline.values()))
                member_baseline = args.get('expected_member_baseline')
                guarded_live_edit = guarded_live_edit or (
                    name in {'plc_delete_member', 'plc_rename_member'}
                    and bool(args.get('path')) and isinstance(member_baseline, dict)
                    and member_baseline.get('path') == args['path']
                    and re.fullmatch(r'[0-9a-f]{64}', str(member_baseline.get('sha256') or '')))
                if name == 'plc_delete_member' and args.get('dry_run') is True:
                    guarded_live_edit = True
                if active.get("saved") is False and not guarded_live_edit:
                    return precondition_failure(
                        status="conflict", condition="source_editor_state",
                        expected={"saved": True, "target": target},
                        actual={"saved": False, "active_document": active},
                        scope={"tool": name, "object": target, "document": full_name},
                        reason="目标 PLC 文档存在未保存编辑，继续写入可能覆盖用户缓冲区。",
                        next_action=("先用 plc_read(name=所属POU,path=精确路径,structure_baseline=true) 读取实时成员基线，携带 member_baseline 作为 expected_member_baseline 再申请本次操作；不自动保存。"
                                     if name in {'plc_delete_member', 'plc_rename_member'} else
                                     "先用 plc_read 读取精确 path/method 的完整实时声明和实现（area=all），基于其内容修改并携带 source_hashes 作为 expected_source_hashes 重新提交。读回不完整或无法合并时请用户保存或处理。禁止添加 direct/force 参数；Agent 不自动保存或丢弃。"),
                    )

    if "plc_object_scope" in names:
        object_name = str(args.get("name") or args.get("pou") or "").strip()
        if not object_name:
            return precondition_failure(
                status="blocked", condition="plc_object_scope",
                expected="non-empty PLC object/member name",
                actual={"name": object_name}, scope={"tool": name},
                reason="PLC 写入/结构变更缺少对象范围。",
                next_action="先用 plc_find/plc_structure 确认对象和精确路径，再重新提交。",
            )

    # An explicitly supplied source hash turns the live baseline into a
    # compare-and-check precondition.  Without it, plc_patch still has its
    # unique old_text guard and full writes use the existing live review.
    expected_hashes = args.get("expected_source_hashes")
    expected_one = args.get("expected_source_hash")
    if source_mutation and (expected_hashes is not None or expected_one is not None):
        try:
            current = ps_com(
                "read-pou", name=args.get("name") or args.get("pou") or "",
                path=args.get("path") or "", method=args.get("method") or "",
                area="all", include_member_code=False, start_line=1, max_lines=0,
            )
            actual_hashes = {
                area: hashlib.sha256(str(current.get(area) or "").encode("utf-8")).hexdigest()
                for area in ("declaration", "implementation") if area in current
                and not current.get(f"{area}_paging", {}).get("truncated")
                and not current.get("truncated") and not current.get("error")
            }
        except Exception as exc:  # noqa: BLE001
            return precondition_failure(
                status="unknown", condition="source_conflict",
                expected=expected_hashes if expected_hashes is not None else expected_one,
                actual={"error": str(exc)},
                scope={"tool": name, "object": args.get("name") or args.get("pou")},
                reason="无法读取带版本校验的 PLC 实时源基线，拒绝猜测当前内容。",
                next_action="用 plc_read_smart(live=true) 或 plc_read_current 重新取得源版本后再提交。",
            )
        expected = expected_hashes if isinstance(expected_hashes, dict) else {
            str(args.get("area") or "implementation"): str(expected_one)
        }
        mismatches = {
            key: {"expected": str(value), "actual": actual_hashes.get(key)}
            for key, value in expected.items()
            if str(value) != str(actual_hashes.get(key) or "")
        }
        if mismatches:
            return precondition_failure(
                status="conflict", condition="source_conflict",
                expected=expected, actual=actual_hashes,
                scope={"tool": name, "object": args.get("name") or args.get("pou"),
                       "path": args.get("path") or ""},
                reason="PLC 源版本已变化，候选修改不再对应当前实时对象。",
                next_action="重新 live 读取并基于最新 revision 生成 patch；禁止自动覆盖或重放。",
                failed_preconditions=[{
                    "condition": "source_conflict", "expected": expected,
                    "actual": actual_hashes, "scope": {
                        "tool": name, "object": args.get("name") or args.get("pou"),
                    }, "reason": "source revision mismatch",
                    "next_action": "refresh live source", "not_executed": True,
                }],
            )

    if source_mutation:
        from tc_agent.editor_offline import check_source_offline
        offline_failure = check_source_offline(args, ps_com)
        if offline_failure is not None:
            return precondition_failure(
                status="blocked" if offline_failure.get('online_runtimes') else "unknown",
                condition="source_editor_state", expected={"logged_in": False},
                actual=offline_failure, scope={"tool": name},
                reason="修改 PLC 代码前必须确认 XAE PLC 编辑器已登出。",
                next_action="先用 plc_editor_state 确认；在线时申请 tc_logout 审批，登出后重读源码基线再修改。不 Stop、不切 Config。",
            )

    if names & {"target_identity", "ads_endpoint", "runtime_selection", "plc_runtime_run"}:
        try:
            target = ps_com("target-show")
            inventory = (
                ps_com("plc-runtimes")
                if names & {"ads_endpoint", "runtime_selection", "plc_runtime_run"}
                else {"plcs": []}
            )
        except Exception as exc:  # noqa: BLE001
            return precondition_failure(
                status="unavailable", condition="target_identity",
                expected="target AMS NetId and PLC runtime inventory",
                actual={"error": str(exc)}, scope={"tool": name},
                reason="无法读取当前目标或 PLC runtime 清单。",
                next_action="先使用 tc_project_info/tc_target_show 和 PLC runtime 只读检查；不自动切换目标。",
            )
        net_id = str((target or {}).get("target_netid") or "").strip() if isinstance(target, dict) else ""
        runtimes = list((inventory or {}).get("plcs") or []) if isinstance(inventory, dict) else []
        if not net_id:
            return precondition_failure(
                status="blocked", condition="target_identity",
                expected="one non-empty target AMS NetId", actual=target,
                scope={"tool": name}, reason="当前工程没有可确认的目标 AMS NetId。",
                next_action="先由用户选择/确认目标，再重新执行；Agent 不自动切换目标。",
            )
        runtime = str(args.get("runtime") or "").strip()
        # This is the final common gate. System-level operations need the
        # target identity above, not a PLC project/port selection. Their
        # mode/scope checks and approval remain in the existing outer policy
        # and handlers. Do not fabricate an empty PLC inventory as a failure.
        if not names & {"ads_endpoint", "runtime_selection", "plc_runtime_run"}:
            return None
        all_plcs = args.get("all_plcs") is True
        if runtime and all_plcs:
            return precondition_failure(
                status="blocked", condition="runtime_selection",
                expected="runtime or all_plcs, not both",
                actual={"runtime": runtime, "all_plcs": all_plcs}, scope={"tool": name},
                reason="runtime 和 all_plcs 互斥，无法确定动作范围。",
                next_action="只保留一个明确的 runtime，或显式确认 all_plcs=true。",
            )
        if runtime:
            selected = [item for item in runtimes
                        if str(item.get("name") or "").casefold() == runtime.casefold()]
        elif all_plcs:
            selected = runtimes
        else:
            selected = runtimes if len(runtimes) == 1 else []
        if not selected:
            status = "blocked" if runtimes else "unavailable"
            return precondition_failure(
                status=status, condition="runtime_selection",
                expected="one exact runtime or explicit all_plcs=true",
                actual={"requested_runtime": runtime, "all_plcs": all_plcs,
                        "candidates": [{"name": x.get("name"), "ads_port": x.get("ads_port")} for x in runtimes]},
                scope={"tool": name, "target_netid": net_id},
                reason=("未找到与请求完全匹配的 PLC runtime。" if runtimes else
                        "当前工程没有可用 PLC runtime 清单。"),
                next_action="先读取 plc-runtimes，明确 runtime 名；多 PLC 不要按 851/852 猜端口。",
            )
        unresolved = [item for item in selected if item.get("ads_port") is None]
        if unresolved and "ads_endpoint" in names:
            return precondition_failure(
                status="unknown", condition="ads_endpoint",
                expected="actual ADS port resolved from the PLC project",
                actual={"target_netid": net_id, "runtimes": unresolved},
                scope={"tool": name},
                reason="PLC runtime 存在，但 Automation Interface 未暴露可确认的 ADS 端口。",
                next_action="先通过 plc-runtimes/项目元数据确认实际端口；禁止按 runtime 顺序猜测。",
            )
        if "plc_runtime_run" in names and len(selected) == 1:
            try:
                state = read_ads_state(net_id, int(selected[0]["ads_port"]))
            except Exception as exc:  # noqa: BLE001
                return precondition_failure(
                    status="unknown", condition="plc_runtime_run",
                    expected={"state_code": 5, "state_name": "Run"},
                    actual={"error": str(exc), "target_netid": net_id,
                            "ads_port": selected[0].get("ads_port")},
                    scope={"tool": name, "runtime": selected[0].get("name")},
                    reason="无法在实际写入前确认 PLC Runtime 的 ADS 状态。",
                    next_action="先只读读取目标 PLC 状态；不要自动登录、停止或切换模式。",
                )
            if not isinstance(state, dict) or state.get("state_code") != 5:
                return precondition_failure(
                    status="blocked", condition="plc_runtime_run",
                    expected={"state_code": 5, "state_name": "Run"}, actual=state,
                    scope={"tool": name, "runtime": selected[0].get("name")},
                    reason="PLC Runtime 当前不是 Run，变量写入未执行。",
                    next_action="由用户明确决定是否切换到 Run；不要为变量写入自动 start/config/restart。",
                )

    return None


_OBJ = lambda props=None, req=None: {  # noqa: E731  JSON schema 简写
    "type": "object", "properties": props or {}, **({"required": req} if req else {})}
_STR = {"type": "string"}
_STATIC_RULE_SEVERITIES = {
    "type": "object", "additionalProperties": {"type": "string", "enum": ["off", "warning", "error"]},
    "description": "可选 SA编号到严重级别的映射；仅按用户明确配置传入，不擅自降级/禁用。仅影响本次 TCSA，不修改 XAE。",
}


def _decode_structured_argument(value, expected: str, path: str):
    """Recover JSON objects/arrays stringified by weak tool-call providers.

    Some OpenAI/Anthropic-compatible gateways return nested JSON Schema values
    as strings even though the outer tool input is valid JSON. Do this once at
    the execution boundary so individual tools do not accidentally treat
    ``"false"`` as true or call ``dict()``/``list()`` on JSON text.
    """
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} 必须是 JSON {expected}，不能是字符串") from exc
    if expected == "object" and not isinstance(decoded, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    if expected == "array" and not isinstance(decoded, list):
        raise ValueError(f"{path} 必须是 JSON 数组")
    return decoded


def _normalize_schema_value(value, schema: dict, path: str):
    expected = schema.get("type")
    if expected in {"object", "array"}:
        value = _decode_structured_argument(value, expected, path)
    elif expected == "boolean" and type(value) is not bool:
        if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
            value = value.strip().casefold() == "true"
        elif type(value) is int and value in {0, 1}:
            value = bool(value)
        else:
            raise ValueError(f"{path} 必须是布尔值 true/false")
    elif expected == "integer" and type(value) is not int:
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            value = int(value.strip())
        else:
            raise ValueError(f"{path} 必须是整数")
    elif expected == "number" and type(value) not in {int, float}:
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError as exc:
                raise ValueError(f"{path} 必须是数值") from exc
        else:
            raise ValueError(f"{path} 必须是数值")

    if expected == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties")
        return {
            key: _normalize_schema_value(
                item,
                props.get(key) or (additional if isinstance(additional, dict) else {}),
                f"{path}.{key}",
            )
            for key, item in value.items()
        }
    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items") or {}
        return [
            _normalize_schema_value(item, item_schema, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _normalize_tool_arguments(name: str, args: dict, schema: dict) -> dict:
    """Normalize provider quirks and a small, documented HMI legacy shape."""
    clean = dict(args)
    if name == "tc_hmi_controls_batch":
        # Older/generated calls used create-view's ``controls``/``id`` names.
        # Accept them at the boundary, but expose and execute only the canonical
        # ``operations``/``control_id`` contract.
        if "operations" not in clean and "controls" in clean:
            clean["operations"] = clean.pop("controls")
        if "operations" in clean:
            operations = _decode_structured_argument(
                clean["operations"], "array", "tc_hmi_controls_batch.operations"
            )
            inherited_action = clean.pop("action", "")
            normalized = []
            for index, operation in enumerate(operations):
                operation = _decode_structured_argument(
                    operation, "object", f"tc_hmi_controls_batch.operations[{index}]"
                )
                operation = dict(operation)
                if "control_id" not in operation and "id" in operation:
                    operation["control_id"] = operation.pop("id")
                if "action" not in operation and inherited_action:
                    operation["action"] = inherited_action
                normalized.append(operation)
            clean["operations"] = normalized
    return _normalize_schema_value(clean, schema, name)


def _run_hmi_write_markup(args: dict):
    """Make a full-page Agent rewrite an explicit, reviewable exception."""
    if "markup" in args and not args.get("acknowledge_full_rewrite", False):
        return {
            "status": "blocked",
            "written": False,
            "error_type": "hmi_full_rewrite_not_acknowledged",
            "reason": (
                "整页源码写入会覆盖现有页面。单控件属性必须用 tc_hmi_control_edit，"
                "同页多个控件必须用 tc_hmi_controls_batch；不能因专用工具参数报错而绕过。"
            ),
            "required": "只有用户明确要求整体替换页面时，才可设置 acknowledge_full_rewrite=true。",
            "recommended_tools": ["tc_hmi_control_edit", "tc_hmi_controls_batch"],
        }
    params = dict(args)
    params.pop("acknowledge_full_rewrite", None)
    params.pop("full_rewrite_reason", None)
    return __import__('tc_template.hmi_candidates', fromlist=['write_markup']).write_markup(params)
_IO_CONFIGURATION = {
    "type": "object",
    "description": "内联 EtherCAT 设备清单；与 manifest 路径二选一",
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "master": {
            "type": "object",
            "properties": {
                "name": _STR,
                "subtype": {"type": "integer"},
            },
            "required": ["name"],
        },
        "devices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": _STR,
                    "name": _STR,
                    "parent": _STR,
                    "subtype": {"type": "integer"},
                    "product_candidates": {"type": "array", "items": _STR},
                    "before": _STR,
                },
                "required": ["id", "name", "parent", "product_candidates"],
            },
        },
    },
    "required": ["schema_version", "master", "devices"],
}


def _target_set(args: dict) -> dict:
    query = str(args.get("target") or "").strip()
    if not query:
        raise ValueError("target 不能为空")
    try:
        route = resolve_target(query)
        netid = route["net_id"]
        matched = {
            "name": route["name"],
            "address": route["address"],
            "matched_by": route["matched_by"],
        }
    except ValueError:
        import re
        if not re.fullmatch(r"(?:\d{1,3}\.){5}\d{1,3}", query):
            raise ValueError(
                "目标必须匹配已配置路由，或使用严格的六段 AMS NetId"
            )
        octets = [int(part) for part in query.split(".")]
        if any(part > 255 for part in octets):
            raise ValueError("AMS NetId 的每一段必须在 0..255 范围内")
        netid = query
        matched = {"matched_by": "raw_netid"}
    result = ps_com("target-set", netid=netid)
    return {**result, **matched}


def _io(command: str, args: dict) -> object:
    configuration = args.get("configuration")
    manifest = args.get("manifest", "")
    if command not in {"export", "remove-master"} and not manifest and configuration is None:
        raise ValueError("必须提供 manifest 路径或 configuration 对象")
    return ps_io_configuration(
        command,
        manifest=manifest,
        configuration=configuration,
        output=args.get("output", ""),
        master=args.get("master", ""),
        allow_existing=bool(args.get("allow_existing", False)),
        confirm_remove=(command == "remove"),
        allow_with_children=bool(args.get("allow_with_children", False)),
        timeout=240.0 if command in {"create", "remove", "esi-check"} else 120.0,
    )


def _scan_devices(_args: dict) -> object:
    target = ps_com("target-show")
    net_id = str(target.get("target_netid") or "")
    try:
        state = read_ads_state(net_id, 300)
    except AdsStateError as exc:
        # TIRS frequently reports only "Started". A failed ADS probe must not
        # recreate the old false-negative: let the scan attempt run, but never
        # switch mode, activate, or restart behind the user's back.
        result = ps_com(
            "io-scan", config_confirmed=False, allow_unknown=True, timeout=180.0)
        if isinstance(result, dict):
            result["state_check_warning"] = str(exc)
        return result
    if not state["is_config"]:
        return {
            "error": (
                f"设备扫描要求目标处于 Config；ADS 实测为 "
                f"{state['state_name']}（{state['state_code']}）。"
                "请先单独执行 tc_config_mode，再重新批准扫描。"
            ),
            "ads_state": state,
        }
    result = ps_com(
        "io-scan", config_confirmed=True, allow_unknown=False, timeout=180.0)
    if isinstance(result, dict):
        result["ads_state"] = state
    return result


def _wait_ads_state(net_id: str, expected: set[int], timeout: float,
                    port: int = 300) -> tuple[dict | None, str]:
    deadline = time.monotonic() + timeout
    last_state = None
    last_error = ""
    while time.monotonic() < deadline:
        try:
            last_state = read_ads_state(net_id, port)
            last_error = ""
            if last_state["state_code"] in expected:
                return last_state, ""
        except AdsStateError as exc:
            last_error = str(exc)
        time.sleep(1.0)
    return last_state, last_error


def _set_config_mode(_args: dict) -> object:
    target = ps_com("target-show")
    net_id = str(target.get("target_netid") or "").strip()
    if not net_id:
        return {"error": "当前系统项目没有目标 AMS NetId"}

    try:
        before = read_ads_state(net_id, ADS_SYSTEM_SERVICE_PORT)
    except AdsStateError as exc:
        before = {"state_code": None, "state_name": "Unavailable", "error": str(exc)}
    if before.get("state_code") in (7, 8, 15):
        return {
            "status": "already_config",
            "target_netid": net_id,
            "state": before,
            "verified": True,
        }

    attempts = []
    try:
        attempts.append({"strategy": "ADS System Service RECONFIG", "result":
                         write_ads_control(net_id, ADS_SYSTEM_SERVICE_PORT,
                                           ADSSTATE_RECONFIG)})
    except AdsStateError as exc:
        attempts.append({"strategy": "ADS System Service RECONFIG", "error": str(exc)})
    state, state_error = _wait_ads_state(
        net_id, {7, 8, 15}, 30.0, port=ADS_SYSTEM_SERVICE_PORT)
    if state and state["state_code"] in (7, 8, 15):
        return {
            "status": "config",
            "target_netid": net_id,
            "strategy": "ADS System Service RECONFIG",
            "before": before,
            "state": state,
            "attempts": attempts,
            "verified": True,
        }
    try:
        attempts.append({"strategy": "consume", "result": ps_com(
            "config-mode", strategy="consume", timeout=30.0)})
    except Exception as exc:
        attempts.append({"strategy": "consume", "error": str(exc)})
    state, state_error = _wait_ads_state(
        net_id, {7, 8, 15}, 10.0, port=ADS_SYSTEM_SERVICE_PORT)
    if state and state["state_code"] in (7, 8, 15):
        return {
            "status": "config",
            "target_netid": net_id,
            "strategy": "TIRS ConsumeXml",
            "before": before,
            "state": state,
            "attempts": attempts,
            "verified": True,
        }

    try:
        attempts.append({"strategy": "command", "result": ps_com(
            "config-mode", strategy="command", timeout=45.0)})
    except Exception as exc:
        attempts.append({"strategy": "command", "error": str(exc)})
    state, state_error = _wait_ads_state(
        net_id, {7, 8, 15}, 30.0, port=ADS_SYSTEM_SERVICE_PORT)
    if state and state["state_code"] in (7, 8, 15):
        return {
            "status": "config",
            "target_netid": net_id,
            "strategy": attempts[-1].get("result", {}).get("strategy", "DTE Config restart"),
            "before": before,
            "state": state,
            "attempts": attempts,
            "verified": True,
        }
    detail = (
        f"ADS 最终状态为 {state['state_name']}（{state['state_code']}）"
        if state else f"ADS 状态无法读取：{state_error or 'unknown error'}"
    )
    return {
        "error": f"已发送两种 Config 切换请求，但未确认切换成功；{detail}",
        "target_netid": net_id,
        "before": before,
        "state": state,
        "attempts": attempts,
        "verified": False,
    }


def _set_run_mode(_args: dict) -> object:
    """Request Run mode and only report success after ADS verification."""
    target = ps_com("target-show")
    net_id = str(target.get("target_netid") or "").strip()
    if not net_id:
        return {"error": "当前系统项目没有目标 AMS NetId"}

    try:
        before = read_ads_state(net_id, ADS_SYSTEM_SERVICE_PORT)
    except AdsStateError as exc:
        before = {"state_code": None, "state_name": "Unavailable", "error": str(exc)}
    if before.get("state_code") == 5:
        return {
            "status": "already_run", "target_netid": net_id,
            "state": before, "verified": True,
        }

    attempts = []
    try:
        attempts.append({"strategy": "ADS System Service RESET", "result":
                         write_ads_control(net_id, ADS_SYSTEM_SERVICE_PORT,
                                           ADSSTATE_RESET)})
    except AdsStateError as exc:
        attempts.append({"strategy": "ADS System Service RESET", "error": str(exc)})
    state, state_error = _wait_ads_state(
        net_id, {5}, 30.0, port=ADS_SYSTEM_SERVICE_PORT)
    if state and state["state_code"] == 5:
        return {
            "status": "run", "target_netid": net_id,
            "strategy": "ADS System Service RESET",
            "before": before, "state": state,
            "attempts": attempts, "verified": True,
        }
    try:
        attempts.append({"strategy": "TIRS ConsumeXml", "result": ps_com(
            "run-mode", timeout=45.0)})
    except Exception as exc:
        attempts.append({"strategy": "TIRS ConsumeXml", "error": str(exc)})

    state, state_error = _wait_ads_state(
        net_id, {5}, 30.0, port=ADS_SYSTEM_SERVICE_PORT)
    if state and state["state_code"] == 5:
        return {
            "status": "run", "target_netid": net_id,
            "before": before, "state": state,
            "attempts": attempts, "verified": True,
        }
    detail = (
        f"ADS 最终状态为 {state['state_name']}（{state['state_code']}）"
        if state else f"ADS 状态无法读取：{state_error or 'unknown error'}"
    )
    return {
        "error": f"已发送 Run 切换请求，但未确认切换成功；{detail}",
        "target_netid": net_id, "before": before, "state": state,
        "attempts": attempts, "verified": False,
    }


def _verify_plc_runtime_states(target_netid: str, expected: set[int],
                               timeout: float = 20.0) -> dict:
    """Verify all configured PLC runtimes by their discovered ADS ports."""
    inventory = ps_com("plc-runtimes")
    runtimes = inventory.get("plcs", []) if isinstance(inventory, dict) else []
    results = []
    for runtime in runtimes:
        port = runtime.get("ads_port")
        item = {**runtime, "verified": False}
        if port is None:
            item["error"] = "PLC ADS port could not be read from the project"
            results.append(item)
            continue
        state, error = _wait_ads_state(target_netid, expected, timeout, port=int(port))
        if state is not None:
            item["state"] = state
            item["verified"] = state.get("state_code") in expected
        if error:
            item["error"] = error
        results.append(item)
    return {
        "target_netid": target_netid,
        "plcs": results,
        "verified": bool(results) and all(item.get("verified") for item in results),
        "skipped": not results,
    }


def _runtime_command(command: str, expected: set[int] | None = None,
                     timeout: float = 20.0, args: dict | None = None) -> object:
    """All frontends use the shared bridge transition contract."""
    return ps_com(command, timeout=timeout, **(args or {}))


_PLC_VALUE_TYPES = {
    "bool", "sint", "usint", "byte", "int", "uint", "word", "dint",
    "udint", "dword", "lint", "ulint", "lword", "real", "lreal",
    "time", "ltime", "date", "tod", "dt",
}
_PLC_SYMBOL_NAME = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*(?:\[-?\d+(?:\s*,\s*-?\d+)*\])?)*$"
)
_PLC_SYMBOL_SCHEMA = {
    "type": "string", "minLength": 1, "pattern": _PLC_SYMBOL_NAME.pattern,
    "description": "必填 ADS 符号全名，例如 MAIN.fbPid.bEnable、GVL_Data.aValues[0]（仅格式示例，先核实实际实例）。不能传 TIPC^ 工程树路径，也不能用 path 代替 name。",
}


def _plc_value_endpoint(args: dict) -> dict:
    """Resolve the current target and exactly one configured PLC runtime."""
    target = ps_com("target-show")
    net_id = str(target.get("target_netid") or "").strip()
    if not net_id:
        raise ValueError("当前系统项目没有目标 AMS NetId")
    inventory = ps_com("plc-runtimes")
    runtimes = list(inventory.get("plcs") or []) if isinstance(inventory, dict) else []
    usable = [item for item in runtimes if item.get("ads_port") is not None]
    requested_port = args.get("ads_port")
    selector = str(args.get("runtime") or "").strip().lower()
    if requested_port is not None:
        port = int(requested_port)
        matches = [item for item in usable if int(item["ads_port"]) == port]
    elif selector:
        def fields(item: dict) -> list[str]:
            return [str(item.get(key) or "").lower() for key in (
                "name", "project", "project_name", "instance", "path", "plc_project",
            )]
        matches = [item for item in usable if any(selector == value for value in fields(item))]
        if not matches:
            matches = [item for item in usable if any(selector in value for value in fields(item))]
    else:
        matches = usable
    if len(matches) != 1:
        choices = [{"name": item.get("name"), "ads_port": item.get("ads_port"),
                    "path": item.get("path")} for item in usable]
        if not matches:
            raise ValueError(f"未找到指定 PLC runtime；可选项：{choices}")
        raise ValueError(f"存在多个 PLC runtime，请传 runtime 或 ads_port 明确选择：{choices}")
    runtime = matches[0]
    return {
        "target_netid": net_id,
        "ads_port": int(runtime["ads_port"]),
        "runtime": runtime,
    }


def _plc_value_items(args: dict, *, writing: bool = False) -> list[dict]:
    items = args.get("values" if writing else "symbols")
    if not isinstance(items, list) or not items:
        raise ValueError("至少提供一个变量")
    if len(items) > 32:
        raise ValueError("单次最多读取或写入 32 个变量")
    normalized = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("变量项必须是对象")
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("type") or "").strip().lower()
        if not _PLC_SYMBOL_NAME.fullmatch(name):
            raise ValueError(f"只允许 PLC 符号名，不允许原始地址或 ADS offset：{name!r}")
        if name.lower() in seen:
            raise ValueError(f"变量重复：{name}")
        if kind not in _PLC_VALUE_TYPES:
            raise ValueError(f"不支持的 PLC 标量类型：{raw.get('type')}")
        if writing and "value" not in raw:
            raise ValueError(f"写入变量 {name} 缺少 value")
        seen.add(name.lower())
        normalized.append({**raw, "name": name, "type": kind})
    return normalized


def _plc_values_match(actual: object, expected: object, kind: str,
                      tolerance: object = None) -> bool:
    try:
        return scalar_values_match(actual, expected, kind, tolerance)[0]
    except ValueNormalizationError:
        return False


def _dynamic_ads_value(result: dict) -> object:
    if "value" in result:
        value = result["value"]
        if isinstance(value, dict) and "name" in value and "value" in value:
            return value["name"]
        return value
    return result.get("leaf_values")


def _task_runtime_info(args: dict) -> dict:
    """Read PlcTaskSystemInfo fields from one explicitly resolved PLC runtime.

    TwinCAT does not always export ``TwinCAT_SystemInfoVarList`` to ADS.  In
    that case this returns an explicit unavailable result instead of silently
    substituting static TIRT configuration values for live measurements.
    """
    endpoint = _plc_value_endpoint(args)
    try:
        plc_state = read_ads_state(endpoint["target_netid"], endpoint["ads_port"])
    except AdsStateError as exc:
        return {"status": "offline", "available": False, "verified": False,
                "error": str(exc), **endpoint}

    configured = ps_com("task-info", max_depth=6)
    configured_tasks = list(configured.get("tasks") or []) if isinstance(configured, dict) else []
    requested = args.get("task_index")
    if requested is not None:
        indexes = [int(requested)]
        if indexes[0] < 0 or indexes[0] > 255:
            raise ValueError("task_index 必须在 0..255 范围内")
    else:
        # PlcTaskSystemInfo is normally one-based. Probe index zero as a
        # compatibility fallback only when index one is not exported.
        indexes = list(range(1, max(1, len(configured_tasks)) + 1))

    fields = (
        "TaskName", "CycleTime", "Priority", "AdsPort", "CycleCount",
        "DcTaskTime", "LastExecTime", "FirstCycle", "CycleTimeExceeded",
        "InCallAfterOutputUpdate", "RTViolation",
    )
    prefixes = ("TwinCAT_SystemInfoVarList._TaskInfo", "_TaskInfo")
    tasks = []
    probe_errors = []
    for index in indexes:
        chosen_prefix = None
        first_result = None
        candidates = [(prefix, index) for prefix in prefixes]
        if requested is None and index == 1:
            candidates += [(prefix, 0) for prefix in prefixes]
        for prefix, candidate_index in candidates:
            symbol = f"{prefix}[{candidate_index}].CycleTime"
            try:
                first_result = read_dynamic_ads_symbol(
                    endpoint["target_netid"], endpoint["ads_port"], symbol, depth=1)
                chosen_prefix = (prefix, candidate_index)
                break
            except DynamicAdsError as exc:
                probe_errors.append({"symbol": symbol, "error": str(exc)})
        if chosen_prefix is None:
            continue
        prefix, actual_index = chosen_prefix
        values = {"CycleTime": _dynamic_ads_value(first_result)}
        field_errors = []
        for field in fields:
            if field == "CycleTime":
                continue
            symbol = f"{prefix}[{actual_index}].{field}"
            try:
                result = read_dynamic_ads_symbol(
                    endpoint["target_netid"], endpoint["ads_port"], symbol, depth=1)
                values[field] = _dynamic_ads_value(result)
            except DynamicAdsError as exc:
                field_errors.append({"field": field, "error": str(exc)})
        cycle = values.get("CycleTime")
        tasks.append({
            "task_index": actual_index,
            "symbol_prefix": prefix,
            "task_name": values.get("TaskName"),
            "cycle_time_100ns": cycle,
            "cycle_time_us": (float(cycle) / 10.0
                              if isinstance(cycle, (int, float)) else None),
            "priority": values.get("Priority"),
            "ads_port": values.get("AdsPort"),
            "cycle_count": values.get("CycleCount"),
            "dc_task_time_raw": values.get("DcTaskTime"),
            "last_exec_time_raw": values.get("LastExecTime"),
            "first_cycle": values.get("FirstCycle"),
            "cycle_time_exceeded": values.get("CycleTimeExceeded"),
            "rt_violation": values.get("RTViolation"),
            "in_call_after_output_update": values.get("InCallAfterOutputUpdate"),
            "field_errors": field_errors,
        })
    if not tasks:
        return {
            "status": "unavailable", "available": False, "verified": False,
            "reason": "PLC runtime 未通过 ADS 导出 TwinCAT_SystemInfoVarList._TaskInfo；请在 PLC 符号配置中启用相应系统信息，静态 TIRT 配置仍可用 tc_task_info 读取。",
            "plc_state": plc_state,
            "configured_tasks": configured_tasks,
            "probes": probe_errors[:8],
            **endpoint,
        }
    return {
        "status": "read", "available": True, "verified": True,
        "plc_state": plc_state, "tasks": tasks,
        "configured_tasks": configured_tasks,
        **endpoint,
    }


def _dynamic_symbol_metadata(result: dict | None) -> dict:
    metadata = (result or {}).get("symbol") if isinstance(result, dict) else None
    return dict(metadata) if isinstance(metadata, dict) else {}


def _dynamic_symbol_category(metadata: dict) -> str:
    return str(metadata.get("category") or metadata.get("kind") or "").strip().lower()


def _dynamic_symbol_kind(metadata: dict) -> str:
    return canonical_kind(metadata.get("type") or metadata.get("base_type") or "")


def _dynamic_values_match(actual: object, expected: object,
                          tolerance: object = None, metadata: dict | None = None,
                          *, allow_unknown: bool = False) -> tuple[bool, object]:
    """Compare a dynamic ADS value using the symbol's actual type metadata."""
    metadata = dict(metadata or {})
    kind = _dynamic_symbol_kind(metadata)
    category = _dynamic_symbol_category(metadata)
    if kind and is_known_scalar_kind(kind):
        if kind.startswith("string") or kind.startswith("wstring"):
            normalize_tolerance(tolerance)
            return (isinstance(actual, str) and isinstance(expected, str)
                    and actual == expected), expected
        return scalar_values_match(actual, expected, kind, tolerance)
    # Enum values returned by the bridge are either their symbolic name or an
    # integer value. Compare representations exactly; never coerce arbitrary
    # strings to False/0.
    if category in {"enum", "enumeration"}:
        normalize_tolerance(tolerance)
        if isinstance(actual, (bool, int, float, str)) and isinstance(expected, type(actual)):
            return actual == expected, expected
        return False, expected
    # Whole arrays/structures are not writable through plc_write_value. For a
    # read assertion, exact recursive JSON equality is the safe supported form.
    if category in {"array", "struct", "structure", "union"}:
        normalize_tolerance(tolerance)
        return actual == expected, expected
    if allow_unknown:
        normalize_tolerance(tolerance)
        return actual == expected, expected
    raise ValueNormalizationError(
        f"无法根据 ADS 符号元数据验证类型 {metadata.get('type') or '<unknown>'}"
    )


def _dynamic_normalize_for_write(value: object, metadata: dict) -> object:
    kind = _dynamic_symbol_kind(metadata)
    category = _dynamic_symbol_category(metadata)
    if kind and is_known_scalar_kind(kind):
        return normalize_scalar(kind, value)
    if category in {"enum", "enumeration"}:
        if isinstance(value, (bool, int, float, str)):
            return value
        raise ValueNormalizationError("枚举值必须是名称或数值")
    if category in {"array", "struct", "structure", "union"}:
        raise ValueNormalizationError("禁止写入整个数组/结构体/联合体")
    raise ValueNormalizationError(
        f"无法根据 ADS 符号元数据规范化类型 {metadata.get('type') or '<unknown>'}"
    )


def _dynamic_readback_value(value: object) -> object:
    if isinstance(value, dict):
        if "value" in value:
            return _dynamic_ads_value(value)
        if "name" in value:
            return value["name"]
    return value


def _dynamic_symbol_name(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    if not _PLC_SYMBOL_NAME.fullmatch(name):
        raise ValueError(f"只允许 PLC 符号名，不允许原始地址或 ADS offset：{name!r}")
    return name


def _plc_read_value(args: dict) -> dict:
    name = _dynamic_symbol_name(args)
    try:
        normalize_tolerance(args.get("tolerance"))
    except ValueNormalizationError as exc:
        return {"status": "invalid_tolerance", "verified": False,
                "error": str(exc), "name": name}
    endpoint = _plc_value_endpoint(args)
    try:
        state = read_ads_state(endpoint["target_netid"], endpoint["ads_port"])
        result = read_dynamic_ads_symbol(
            endpoint["target_netid"], endpoint["ads_port"], name,
            depth=int(args.get("max_depth") or 3),
        )
    except (AdsStateError, DynamicAdsError) as exc:
        return {"status": "offline", "verified": False, "error": str(exc),
                "name": name, **endpoint}
    actual = _dynamic_ads_value(result)
    verified = None
    normalized_expected = None
    verification_error = None
    if "expected" in args:
        try:
            verified, normalized_expected = _dynamic_values_match(
                actual, args["expected"], args.get("tolerance"),
                _dynamic_symbol_metadata(result),
            )
        except ValueNormalizationError as exc:
            verified, verification_error = False, str(exc)
    return {"status": "read", "plc_state": state, "value": actual,
            "metadata": result.get("symbol"), "verified": verified,
            "expected": args.get("expected"),
            "normalized_expected": normalized_expected,
            "verification_error": verification_error, **endpoint}


def _plc_write_value(args: dict) -> dict:
    name = _dynamic_symbol_name(args)
    if "value" not in args:
        raise ValueError("写入缺少 value")
    try:
        normalize_tolerance(args.get("tolerance"))
    except ValueNormalizationError as exc:
        return {"status": "invalid_tolerance", "written": False,
                "verified": False, "error": str(exc), "name": name}
    endpoint = _plc_value_endpoint(args)
    try:
        state = read_ads_state(endpoint["target_netid"], endpoint["ads_port"])
    except AdsStateError as exc:
        return {"status": "offline", "written": False, "verified": False,
                "error": str(exc), "name": name, **endpoint}
    if state.get("state_code") != 5:
        return {"status": "blocked", "written": False, "verified": False,
                "reason": "PLC runtime 必须处于 Run 才允许写变量",
                "plc_state": state, "name": name, **endpoint}
    try:
        before_result = read_dynamic_ads_symbol(
            endpoint["target_netid"], endpoint["ads_port"], name, depth=1)
    except DynamicAdsError as exc:
        return {"status": "failed", "written": False, "verified": False,
                "error": str(exc), "failure_stage": "pre_write_read",
                "plc_state": state, "name": name, **endpoint}
    before = _dynamic_ads_value(before_result)
    metadata = _dynamic_symbol_metadata(before_result)
    normalized_value = args["value"]
    if metadata:
        try:
            normalized_value = _dynamic_normalize_for_write(args["value"], metadata)
        except ValueNormalizationError as exc:
            return {"status": "invalid_value", "written": False,
                    "verified": False, "error": str(exc),
                    "before": before, "metadata": metadata,
                    "name": name, **endpoint}
    if "expected_before" in args:
        try:
            before_matches, normalized_before = _dynamic_values_match(
                before, args["expected_before"], args.get("tolerance"),
                metadata, allow_unknown=not bool(metadata),
            )
        except ValueNormalizationError as exc:
            return {"status": "invalid_value", "written": False,
                    "verified": False, "error": str(exc),
                    "before": before, "metadata": metadata,
                    "name": name, **endpoint}
        if not before_matches:
            return {"status": "precondition_failed", "written": False,
                    "verified": False, "before": before,
                    "expected_before": args["expected_before"],
                    "normalized_expected_before": normalized_before,
                    "metadata": metadata, "plc_state": state,
                    "name": name, **endpoint}
    try:
        result = write_dynamic_ads_symbol(
            endpoint["target_netid"], endpoint["ads_port"], name, normalized_value, depth=1)
    except DynamicAdsError as exc:
        return {"status": "write_result_unknown", "written": "unknown",
                "verified": False, "retry_safe": False, "error": str(exc),
                "next_action": "写入结果未知，禁止自动重试；先只读确认变量状态。",
                "plc_state": state, "name": name, **endpoint}
    result = dict(result or {})
    metadata = _dynamic_symbol_metadata(result) or metadata
    if result.get("readback_error") or result.get("readback_available") is False:
        return {"status": "written_readback_unavailable", "written": True,
                "verified": False, "retry_safe": False, "before": before,
                "requested": args["value"], "normalized_expected": normalized_value,
                "readback": None, "metadata": metadata,
                "error": str(result.get("readback_error") or "写入后回读不可用"),
                "next_action": "写入已发生但回读不可用，禁止自动重试；先恢复只读回读证据。",
                "plc_state": state, "name": name, **endpoint}
    if "readback" not in result:
        return {"status": "written_readback_unavailable", "written": True,
                "verified": False, "retry_safe": False, "before": before,
                "requested": args["value"], "normalized_expected": normalized_value,
                "readback": None, "metadata": metadata,
                "error": "写入接口未返回回读值，不能声明验证通过",
                "next_action": "写入已发生但没有回读证据，禁止自动重试。",
                "plc_state": state, "name": name, **endpoint}
    readback = _dynamic_readback_value(result.get("readback"))
    try:
        verified, normalized_expected = _dynamic_values_match(
            readback, args["value"], args.get("tolerance"), metadata,
        )
    except ValueNormalizationError as exc:
        return {"status": "written_verification_unavailable", "written": True,
                "verified": False, "retry_safe": False, "before": before,
                "requested": args["value"], "normalized_expected": normalized_value,
                "readback": readback, "metadata": metadata, "error": str(exc),
                "next_action": "类型未知，不能把本次写入声明为已验证；禁止自动重试。",
                "plc_state": state, "name": name, **endpoint}
    return {"status": "verified" if verified else "readback_mismatch",
            "written": True, "verified": verified,
            "retry_safe": True if verified else False, "before": before,
            "requested": args["value"], "normalized_expected": normalized_expected,
            "readback": readback, "metadata": metadata, "plc_state": state,
            "next_action": ("回读一致。" if verified else
                            "已写入但回读不一致，禁止自动重试；先报告实际差异。"),
            "name": name, **endpoint}


def _plc_read_values(args: dict) -> dict:
    endpoint = _plc_value_endpoint(args)
    items = _plc_value_items(args)
    try:
        for item in items:
            normalize_tolerance(item.get("tolerance"))
    except ValueNormalizationError as exc:
        return {"status": "invalid_tolerance", "verified": False,
                "error": str(exc)}
    requested = {item["name"]: item["type"] for item in items}
    try:
        state = read_ads_state(endpoint["target_netid"], endpoint["ads_port"])
        values = read_ads_values_by_name(
            endpoint["target_netid"], endpoint["ads_port"], requested)
    except AdsStateError as exc:
        return {
            "status": "offline", "verified": False, "error": str(exc),
            "requested_symbols": requested, **endpoint,
        }
    results = []
    assertions = []
    for item in items:
        result = {"name": item["name"], "type": item["type"],
                  "value": values[item["name"]]}
        if "expected" in item:
            result["expected"] = item["expected"]
            try:
                result["matched"], result["normalized_expected"] = scalar_values_match(
                    result["value"], item["expected"], item["type"], item.get("tolerance"))
            except ValueNormalizationError as exc:
                result["matched"] = False
                result["verification_error"] = str(exc)
            assertions.append(result["matched"])
        results.append(result)
    return {
        "status": "read" if all(assertions) or not assertions else "assertion_failed",
        "target_netid": endpoint["target_netid"],
        "ads_port": endpoint["ads_port"], "runtime": endpoint["runtime"],
        "plc_state": state, "values": results,
        "verified": all(assertions) if assertions else None,
        "assertion_count": len(assertions),
    }


def _plc_verify_workflow(args: dict) -> dict:
    """Deterministic compile -> static analysis -> optional runtime assertions."""
    stages: list[dict] = []
    live_code: list[dict] | None = None
    try:
        live_code = ps_com("all-code")
        if not isinstance(live_code, list) or not live_code:
            live_code = None
            raise ValueError("未读取到可验证的 PLC 对象，不能将空结果视为校准成功")
        stages.append({
            "stage": "source_calibration", "ok": True,
            "result": {"source": "live_com", "objects": len(live_code)},
        })
    except Exception as exc:
        stages.append({
            "stage": "source_calibration", "ok": False,
            "result": {"error": str(exc), "exception": type(exc).__name__},
        })
    try:
        # The build is an effectful operation.  execute_plc_build performs the
        # approval-bound state recheck and is the only path allowed to call
        # com_build; plc_verify must not retain a private compile bypass.
        build = execute_plc_build(args)
        build_ok = build.get('compiler_verified') is True
        stages.append({"stage": "build", "ok": build_ok, "result": build})
    except Exception as exc:  # preserve later diagnostics and stage identity
        build_ok = False
        stages.append({
            "stage": "build", "ok": False,
            "result": {"error": str(exc), "exception": type(exc).__name__},
        })

    try:
        if live_code is None:
            raise RuntimeError("实时 XAE 源码校准失败，禁止对磁盘缓存执行验证")
        analysis = analyze_objects(
            live_code,
            max_complexity=max(1, int(args.get("max_complexity") or 20)),
            rule_severities=args.get("rule_severities"),
        )
        static_errors = (analysis.get("summary") or {}).get("errors")
        static_ok = tool_succeeded(analysis) and type(static_errors) is int and static_errors == 0
        stages.append({"stage": "static_analysis", "ok": static_ok, "result": analysis})
    except Exception as exc:
        static_ok = False
        stages.append({
            "stage": "static_analysis", "ok": False,
            "result": {"error": str(exc), "exception": type(exc).__name__},
        })

    symbols = list(args.get("symbols") or [])
    runtime_ok: bool | None = None
    if symbols and build_ok and static_ok:
        runtime_args = {"symbols": symbols}
        if args.get("runtime"):
            runtime_args["runtime"] = args["runtime"]
        if args.get("ads_port"):
            runtime_args["ads_port"] = args["ads_port"]
        try:
            runtime_result = _plc_read_values(runtime_args)
            runtime_ok = tool_succeeded(runtime_result) and runtime_result.get("verified") is True
        except Exception as exc:
            runtime_result = {"error": str(exc)}
            runtime_ok = False
        stages.append({
            "stage": "runtime_assertions", "ok": runtime_ok,
            "result": runtime_result,
        })
    elif symbols:
        stages.append({
            "stage": "runtime_assertions", "ok": False, "skipped": True,
            "reason": "编译或静态分析硬错误未通过，禁止用旧 Runtime 结果冒充新代码验证",
        })

    calibration_ok = live_code is not None
    source_verified = calibration_ok and build_ok and static_ok
    # Compilation is not deployment; there is no matching online build identity.
    verified = source_verified and not symbols
    return {
        "workflow": "plc_verify_v2",
        "status": "verified" if verified else ("incomplete" if source_verified and runtime_ok else "failed"),
        "verified": verified,
        "source_verified": source_verified,
        "runtime_values_verified": runtime_ok,
        "runtime_matches_build": None,
        "verification_scope": "source_compile_and_agent_static_analysis",
        "stages": stages,
        "gate": {
            "source_calibration": calibration_ok,
            "build": build_ok, "static_analysis": static_ok,
            "runtime_assertions": runtime_ok,
        },
        "note": (
            "运行时断言未请求；当前结论仅覆盖编译和 Agent 静态分析。"
            if not symbols else "在线值断言与源码验证分别报告；未确认运行程序与本次构建身份一致，不能宣称本次修改已在线验证。不会自动下载或启动 PLC。"
        ),
    }


def _plc_write_values(args: dict) -> dict:
    endpoint = _plc_value_endpoint(args)
    items = _plc_value_items(args, writing=True)
    try:
        normalized_values = {
            item["name"]: normalize_scalar(item["type"], item["value"])
            for item in items
        }
    except ValueNormalizationError as exc:
        return {"status": "invalid_value", "written": False,
                "verified": False, "error": str(exc), **endpoint}
    try:
        for item in items:
            normalize_tolerance(item.get("tolerance"))
    except ValueNormalizationError as exc:
        return {"status": "invalid_tolerance", "written": False,
                "verified": False, "error": str(exc), **endpoint}
    try:
        state = read_ads_state(endpoint["target_netid"], endpoint["ads_port"])
    except AdsStateError as exc:
        return {
            "status": "offline", "written": False, "verified": False,
            "error": str(exc), **endpoint,
        }
    if state.get("state_code") != 5:
        return {
            "status": "blocked", "written": False, "verified": False,
            "reason": "PLC runtime 必须处于 Run 才允许写变量",
            "plc_state": state, **endpoint,
        }
    types = {item["name"]: item["type"] for item in items}
    try:
        before = read_ads_values_by_name(
            endpoint["target_netid"], endpoint["ads_port"], types)
    except AdsStateError as exc:
        return {"status": "failed", "written": False, "verified": False,
                "failure_stage": "pre_write_read", "error": str(exc), **endpoint}
    failed_preconditions = []
    for item in items:
        if "expected_before" in item:
            try:
                matched, normalized = scalar_values_match(
                    before[item["name"]], item["expected_before"], item["type"],
                    item.get("tolerance"),
                )
            except ValueNormalizationError as exc:
                return {"status": "invalid_value", "written": False,
                        "verified": False, "error": str(exc),
                        "before": before, **endpoint}
            if not matched:
                failed_preconditions.append({
                    "name": item["name"], "actual": before[item["name"]],
                    "expected_before": item["expected_before"],
                    "normalized_expected_before": normalized,
                })
    if failed_preconditions:
        return {
            "status": "precondition_failed", "written": False, "verified": False,
            "target_netid": endpoint["target_netid"], "ads_port": endpoint["ads_port"],
            "runtime": endpoint["runtime"], "plc_state": state,
            "failed_preconditions": failed_preconditions,
        }
    requested = {item["name"]: (item["type"], normalized_values[item["name"]]) for item in items}
    try:
        accepted = write_ads_values_by_name(
            endpoint["target_netid"], endpoint["ads_port"], requested)
    except AdsStateError as exc:
        return {"status": "write_result_unknown", "written": "unknown",
                "verified": False, "retry_safe": False, "before": before,
                "error": str(exc), **endpoint}
    try:
        readback = read_ads_values_by_name(
            endpoint["target_netid"], endpoint["ads_port"], types)
    except AdsStateError as exc:
        return {"status": "written_readback_unavailable", "written": True,
                "verified": False, "retry_safe": False, "before": before,
                "accepted": accepted, "error": str(exc), **endpoint}
    results = []
    for item in items:
        matched, normalized_expected = scalar_values_match(
            readback[item["name"]], item["value"], item["type"], item.get("tolerance"))
        results.append({
            "name": item["name"], "type": item["type"],
            "before": before[item["name"]], "requested": item["value"],
            "normalized_expected": normalized_expected,
            "write_accepted": accepted[item["name"]],
            "readback": readback[item["name"]], "matched": matched,
        })
    verified = all(item["matched"] for item in results)
    return {
        "status": "verified" if verified else "written_readback_mismatch",
        "written": True, "verified": verified,
        "retry_safe": True if verified else False,
        "target_netid": endpoint["target_netid"], "ads_port": endpoint["ads_port"],
        "runtime": endpoint["runtime"], "plc_state": state, "values": results,
        "note": ("回读不一致通常表示 PLC 周期程序已覆盖该值；写请求本身仍可能成功。"
                 if not verified else "写入后回读一致。"),
        "next_action": ("回读一致。" if verified else
                        "已写入但回读不一致，禁止自动重试；先报告各项实际差异。"),
    }


def _review_pou_write(args: dict, *, patch: bool = False) -> dict:
    """Review the full post-write POU without changing XAE.

    The Agent is the user-facing authoring path, so its write tools all pass
    through this function before the COM mutation is sent.  The low-level COM
    bridge remains intentionally generic for trusted import/template flows.
    """
    name = str(args["name"])
    area = str(args.get("area") or "implementation")
    method = str(args.get("method") or "")
    current = ps_com(
        "read-pou", name=name, path=args.get("path", ""), method=method,
        area="all", include_member_code=False, start_line=1, max_lines=0,
    )
    if any(current.get(key, {}).get('truncated') for key in
           ('declaration_paging', 'implementation_paging')):
        return {'approved': False, 'blocking_findings': [{
            'rule': 'incomplete-write-baseline', 'severity': 'error',
            'message': 'Live source is truncated; cannot review an incomplete object.'}],
            'advisories': []}
    item_type = int(current.get("itemType") or 0)
    folder = "Interfaces" if item_type == 618 else (
        "DUTs" if item_type in {605, 606, 607, 623} else (
            "GVLs" if item_type == 615 else "POUs"
        )
    )
    candidate = {
        "name": current.get("name") or name,
        "folder": folder,
        "declaration": current.get("declaration") or "",
        "implementation": current.get("implementation") or "",
        "methods": current.get("methods") or [],
    }
    baseline_review = None
    paired = 'implementation' in args and not patch
    if paired and (area != 'declaration' or method or item_type not in {602, 603, 604}):
        raise ValueError('Paired write requires a top-level POU and area=declaration')
    pair_baseline = [candidate['declaration'], candidate['implementation']]
    if patch:
        old = str(args.get("old_text") or "")
        existing = candidate.get(area) or ""
        occurrences = existing.count(old)
        if not old or occurrences != 1:
            # Preserve the backend's precise error rather than producing a
            # misleading review of a candidate that cannot be applied.
            return {"approved": True, "deferred": True,
                    "reason": "patch target validation is performed by COM"}
        baseline_review = review_write_candidate(candidate, changed_area=area)
        candidate[area] = existing.replace(old, str(args.get("new_text") or ""), 1)
    else:
        # Full-area writes must use the same before/after delta gate as local
        # patches. Existing projects commonly contain migration warnings;
        # those are evidence to report, not reasons to reject an unrelated
        # replacement. New or worsened findings still block normally.
        baseline_review = review_write_candidate(candidate, changed_area=area)
        candidate[area] = str(args.get("code") or "")
        if paired:
            candidate['implementation'] = args['implementation']
            if not candidate['declaration'].strip():
                raise ValueError('Paired candidate declaration must not be empty')
            baseline_review = None  # Full paired replacement must pass full review.
            area = 'all'
    review = review_write_candidate(candidate, changed_area=area)
    if baseline_review is not None:
        review = _delta_patch_review(baseline_review, review)
    if method:
        parent = ps_com('read-pou', name=name, path=args.get('path', ''), area='declaration',
                        include_member_code=False, start_line=1, max_lines=0)
        if 'declaration' not in parent or parent.get('declaration_paging', {}).get('truncated'):
            return {'approved': False, 'blocking_findings': [{'rule': 'semantic-unresolved',
                    'severity': 'error', 'message': 'Complete parent declaration is required.'}], 'advisories': []}
        candidate['enclosing_declaration'] = parent['declaration']
    _attach_plc_dependencies(review, candidate, current.get('path', ''))
    review.update({"name": candidate["name"], "method": method, "path": current.get("path", "")})
    if patch:
        review['_expected_area'] = candidate[area]
    if paired:
        review['_paired_baseline'] = pair_baseline
    return review


def _attach_plc_dependencies(review, candidate, path):
    if any(str(f.get('rule', '')).startswith('syntax-')
           for f in review.get('blocking_findings', [])):
        review['approved'] = False
        review['project_context'] = {'status': 'not_checked', 'reason': 'syntax_errors', 'compiler_verified': False}
        return
    from tc_template.plc_write_context import review_dependencies
    context = review_dependencies(candidate, path, ps_com, semantic=True)
    _attach_library_signature_evidence(context, path)
    review['project_context'] = context
    review['blocking_findings'].extend(f for f in context['findings'] if f['severity'] == 'error')
    review['advisories'].extend(f for f in context['findings'] if f['severity'] != 'error')
    review['approved'] = not review['blocking_findings']


def _attach_library_signature_evidence(context, path):
    names = {str(f.get('message', '')).split(': ', 1)[-1].upper()
             for f in context.get('findings', [])
             if str(f.get('message', '')).startswith('Unresolved declaration/library symbol: ')}
    if not names or not str(path).startswith('TIPC^'):
        return
    try:
        result = ps_com('library-evidence', path='^'.join(path.split('^')[:3]))
        matches = []
        for lib in result.get('libraries', []):
            known = {str(n).upper() for n in lib.get('symbols', [])}
            matched = sorted(names & known)
            if matched:
                matches.append({k: lib.get(k) for k in ('name', 'effective_version', 'file', 'signature_verified', 'reason')} | {'symbols': matched})
        if matches:
            context.setdefault('semantic_evidence', {})['missing_library_signatures'] = matches
            context['semantic_evidence']['next_action'] = '已确认这些符号属于当前实际引用版本的库，但完整参数签名未取得；补签名读取接口，不要重装库、重命名变量或绕过门禁。'
    except Exception as exc:
        context.setdefault('semantic_evidence', {})['library_evidence_error'] = str(exc)[:300]


def _plc_preflight(args):
    from tc_template.plc_preflight import review_candidates
    result = review_candidates(args.get('candidates'), ps_com)
    for candidate in result.get('candidates', []):
        _attach_library_signature_evidence(candidate.get('review', {}), candidate.get('path', ''))
    return result


def _creation_hint(args: dict) -> dict:
    """Return a concrete creation recommendation for a missing top-level POU."""
    code = str(args.get("code") or "")
    upper = code.upper()
    suggested_type = "fb"
    if re.search(r'\bPROGRAM\b', upper):
        suggested_type = "program"
    elif re.search(r'\bTYPE\s+\w+\s*:\s*STRUCT\b|\bSTRUCT\b', upper):
        suggested_type = "struct"
    elif re.search(r'\bTYPE\s+\w+\s*:\s*\(', upper):
        suggested_type = "enum"
    elif re.search(r'\bTYPE\s+\w+\s*:\s*UNION\b|\bUNION\b', upper):
        suggested_type = "union"
    elif re.search(r'\bVAR_GLOBAL\b|\bGVL_', upper):
        suggested_type = "gvl"
    return {
        "recommended_tool": "plc_create",
        "suggested_args": {
            "name": args["name"], "type": suggested_type,
            "declaration": code if args.get("area") == "declaration" else "",
            "implementation": code if args.get("area") == "implementation" else "",
        },
    }


_NON_BYPASSABLE_REVIEW_RULES = {
    "interface-terminator", "interface-member-inline",
    "method-declaration-header", "method-return-type",
}


def _finding_fingerprint(finding: dict) -> tuple[str, str, str, str, str]:
    """Identify a finding without volatile source coordinates."""
    return tuple(
        str(finding.get(key) or "")
        for key in ("rule", "severity", "object", "area", "message")
    )


def _delta_patch_review(baseline: dict, candidate: dict) -> dict:
    """Make a patch responsible only for newly introduced quality debt.

    A local patch can shift every following line, so source coordinates are
    deliberately excluded from matching. Structural findings that can create
    an invalid XAE object tree remain blocking even when already present.
    """
    before = list(baseline.get("blocking_findings") or [])
    after = list(candidate.get("blocking_findings") or [])
    available = Counter(_finding_fingerprint(item) for item in before)
    historical: list[dict] = []
    introduced: list[dict] = []

    for item in after:
        fingerprint = _finding_fingerprint(item)
        if (item.get("rule") in _NON_BYPASSABLE_REVIEW_RULES
                or str(item.get('rule', '')).startswith('syntax-')):
            introduced.append(item)
        elif available[fingerprint] > 0:
            # Existing diagnostics are report-only. A replacement is
            # responsible for newly introduced/worsened findings; it must not
            # be held hostage by a warning (or an old compiler error) that was
            # already present before the write.
            historical.append(item)
            available[fingerprint] -= 1
        else:
            introduced.append(item)

    review = dict(candidate)
    review.update({
        "approved": not introduced,
        "delta_review": True,
        "baseline_summary": baseline.get("summary") or {},
        "post_patch_summary": candidate.get("summary") or {},
        "full_blocking_findings": after,
        "blocking_findings": introduced,
        "historical_findings": historical,
        "advisories": list(candidate.get("advisories") or []) + historical,
    })
    return review


def _quality_gate_enabled() -> bool:
    """Return the persisted soft PLC quality-gate state."""
    from tc_agent import config as config_module

    return bool(config_module.load_config().get("quality_gate_enabled", True))


def _quality_review_blocks(review: dict) -> bool:
    """Apply the soft gate while retaining non-bypassable safety findings."""
    if review.get("approved"):
        return False
    findings = list(review.get("blocking_findings") or [])
    hard = [
        item for item in findings
        if str(item.get("severity") or "").lower() == "error"
        or item.get("rule") in _NON_BYPASSABLE_REVIEW_RULES
    ]
    if hard:
        review["gate"] = "hard-blocked"
        review["non_bypassable_findings"] = hard
        return True
    if _quality_gate_enabled():
        review["gate"] = "enabled"
        return True
    review["originally_approved"] = False
    review["approved"] = True
    review["gate"] = "bypassed"
    review["bypassed_findings"] = findings
    return False


def _live_write_readback(args: dict, *, expected: str | None = None,
                         old_text: str | None = None,
                         new_text: str = "") -> dict:
    """Authoritatively read the changed XAE area and prove the mutation landed."""
    area = str(args.get("area") or "implementation")
    readback = ps_com(
        "read-pou", name=args["name"], area=area,
        method=args.get("method", ""), path=args.get("path", ""),
        include_member_code=False, start_line=1, max_lines=0,
    )
    actual = str(readback.get(area) or "")
    digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
    if expected is not None:
        verified = actual == expected
        criterion = "exact_area_match"
        if not verified:
            # XAE normalizes CRLF and omits the final editor line terminator.
            # Do not strip spaces, comments, or any interior source differences.
            normalize = lambda value: value.replace("\r\n", "\n").rstrip("\n")
            verified = normalize(actual) == normalize(expected)
            criterion = "editor_line_endings_match"
    else:
        verified = old_text not in actual and (not new_text or new_text in actual)
        criterion = "patch_old_absent_new_present"
    return {
        "verified": verified, "criterion": criterion, "source": "live_com",
        "area": area, "chars": len(actual), "sha256": digest,
        "path": readback.get("path", ""), "method": args.get("method", ""),
    }


def _plc_mutation_error(exc, stage, written):
    login_error = editor_login_failure(exc)
    if login_error:
        return {**login_error, 'failure_stage': stage, 'written': written,
                'verified': False, 'compiler_verified': False}
    return {'status': 'uncertain', 'error': str(exc), 'failure_stage': stage,
            'written': written, 'verified': False, 'compiler_verified': False,
            'retry_safe': False,
            'next_action': 'Read the exact live object before retrying; a failed response does not prove the write did not occur.'}


def _plc_editor_state(args: dict) -> dict:
    state = ps_com("plc-online-state", runtime=args.get("runtime") or "")
    logged_in = state.get("logged_in") if isinstance(state, dict) else None
    known = isinstance(logged_in, bool)
    return {"status": "read" if known else "unknown", "verified": known,
            "editor_logged_in": logged_in if known else None,
            "runtime_state": "not_queried", "evidence": state,
            "next_action": "编译和修改代码前须确认 editor_logged_in=false；在线时先申请 tc_logout 审批并回读。Logout 不等于 Stop，不切换运行模式。"}


def _guarded_plc_write(args: dict) -> dict:
    try:
        review = _review_pou_write(args)
    except FileNotFoundError as exc:
        return {
            "status": "not_found", "written": False,
            "error": f"对象 '{args['name']}' 尚未创建，不能使用 plc_write。请先用 plc_create 新建它。",
            "detail": str(exc), **_creation_hint(args),
        }
    paired_baseline = review.pop('_paired_baseline', None)
    if _quality_review_blocks(review):
        return {"status": "blocked", "written": False, "review": review,
                "failure_stage": "pre_write_review", "compiler_verified": False}
    args = {**args, 'path': review.get('path') or args.get('path', '')}
    if 'implementation' in args:
        try:
            result = ps_com('write-pou', name=args['name'], path=args['path'], area='declaration',
                            code=args['code'], paired_implementation=args['implementation'],
                            paired_baseline=paired_baseline)
        except (RuntimeError, ValueError, OSError) as exc:
            return _plc_mutation_error(exc, 'paired_write', None)
        return {**result, 'review': review, 'compiler_verified': False}
    try:
        result = ps_com("write-pou", name=args["name"], area=args["area"],
                        code=args["code"], method=args.get("method", ""),
                        path=args.get("path", ""))
    except (RuntimeError, ValueError, OSError) as exc:
        return _plc_mutation_error(exc, 'com_write', None)
    try:
        readback = _live_write_readback(args, expected=str(args["code"]))
    except (RuntimeError, ValueError, OSError) as exc:
        return _plc_mutation_error(exc, 'post_write_readback', True)
    return {"status": "written" if readback["verified"] else "verification_failed",
            "written": True, "verified": readback["verified"],
            "verification_scope": "saved_text_readback_only",
            "result": result, "readback": readback, "review": review,
            "compiler_verified": False,
            "failure_stage": None if readback['verified'] else 'post_write_readback',
            "next_action": 'Build for compiler verification.' if readback['verified'] else
                           'Read the live object before retrying; the write was already sent.'}


def _guarded_plc_patch(args: dict) -> dict:
    try:
        review = _review_pou_write(args, patch=True)
    except FileNotFoundError as exc:
        return {
            "status": "not_found", "written": False,
            "error": f"对象 '{args['name']}' 尚未创建，不能使用 plc_patch。请先用 plc_create 新建它。",
            "detail": str(exc), **_creation_hint(args),
        }
    expected = review.pop('_expected_area', None)
    if _quality_review_blocks(review):
        return {"status": "blocked", "written": False, "review": review,
                "failure_stage": "pre_write_review", "compiler_verified": False}
    if expected is None:
        return {'status': 'blocked', 'written': False,
                'error': 'Patch target is not unique; read the exact object and submit one unique old_text.'}
    args = {**args, 'path': review.get('path') or args.get('path', '')}
    try:
        result = ps_com("patch-pou", name=args["name"], area=args["area"],
                        old_text=args["old_text"], new_text=args.get("new_text", ""),
                        method=args.get("method", ""), path=args.get("path", ""))
    except (RuntimeError, ValueError, OSError) as exc:
        return _plc_mutation_error(exc, 'com_write', None)
    try:
        readback = _live_write_readback(args, expected=expected)
    except (RuntimeError, ValueError, OSError) as exc:
        return _plc_mutation_error(exc, 'post_write_readback', True)
    return {"status": "patched" if readback["verified"] else "verification_failed",
            "written": True, "verified": readback["verified"],
            "verification_scope": "saved_text_readback_only",
            "result": result, "readback": readback, "review": review,
            "compiler_verified": False,
            "failure_stage": None if readback['verified'] else 'post_write_readback',
            "next_action": 'Build for compiler verification.' if readback['verified'] else
                           'Read the live object before retrying; the write was already sent.'}


def _review_existing_pou(args: dict) -> dict:
    current = ps_com("read-pou", name=args["name"], path=args.get("path", ""),
                     area="all", include_member_code=True)
    item_type = int(current.get("itemType") or 0)
    folder = "Interfaces" if item_type == 618 else (
        "DUTs" if item_type in {605, 606, 607, 623} else (
            "GVLs" if item_type == 615 else "POUs"
        )
    )
    candidate = {
        "name": current.get("name") or args["name"], "folder": folder,
        "declaration": current.get("declaration") or "",
        "implementation": current.get("implementation") or "",
        "methods": current.get("methods") or [],
    }
    review = review_write_candidate(candidate, changed_area="all")
    review.update({"name": candidate["name"], "path": current.get("path", "")})
    return review


def _plc_read_request(args: dict) -> dict:
    """Normalize a POU path that accidentally includes its member child.

    ``plc_find`` returns the POU path, but users often paste a tree path ending
    in ``...^FB^Action``. Treat the suffix as the member path instead of
    asking COM to resolve the member as a top-level POU.
    """
    from tc_agent.plc_path_contract import identity
    from tc_template.plc_read_contract import validate_object_request
    validate_object_request(str(args.get('name') or '').strip(),
                            str(args.get('tree_path') or args.get('path') or args.get('source_path') or '').strip())
    request, _ = identity(args)
    name = str(request.get("name") or "").strip()
    from tc_template.plc_read_contract import validate_object_request
    path = str(request.get('path') or '').strip()
    source_path = str(request.get('source_path') or '').strip()
    if source_path and path and source_path != path:
        raise ValueError("path 与 source_path 不能同时指定不同值；二者分别用于 COM 树和已保存索引")
    if source_path and not path:
        # source_path is an explicit read-only alias.  It is accepted only by
        # read tools; write schemas do not expose it, so it cannot leak into a
        # plc_write/plc_patch request.
        path = source_path
    validate_object_request(name, path)
    method = str(request.get("method") or "").strip()
    if path and name:
        parts = path.split("^")
        indexes = [i for i, part in enumerate(parts)
                   if part.casefold() == name.casefold()]
        if indexes:
            object_index = indexes[-1]
            suffix = parts[object_index + 1:]
            if suffix:
                if not method:
                    method = ".".join(suffix)
                path = "^".join(parts[:object_index + 1])
    request["path"] = path
    request.pop("source_path", None)
    request["method"] = method
    return request


def _annotate_plc_read_identity(result: dict, *, saved: bool = False) -> dict:
    """Make saved-file and Automation Interface paths non-interchangeable."""
    if not isinstance(result, dict):
        return result
    # Preserve minimal/legacy bridge replies (and test doubles) that contain
    # no path identity at all; there is nothing to classify in such a reply.
    if not saved and not any(result.get(key) for key in
                             ("path", "tree_path", "source", "active_document")):
        return result
    if saved or result.get("source") in {"disk", "disk_index"}:
        source_path = str(result.get("source_path") or result.get("path") or "")
        result.update({
            "source_path": source_path,
            "path_kind": "saved_source",
            "tree_path": "",
            "tree_path_available": False,
            "write_path_available": False,
        })
        return result
    tree_path = str(result.get("tree_path") or result.get("path") or "")
    result.update({
        "tree_path": tree_path,
        "path_kind": "com_tree" if tree_path else "unknown",
        "tree_path_available": bool(tree_path),
        "write_path_available": bool(tree_path),
    })
    result.setdefault('source_path', '')
    if (result.get('active_document') or {}).get('source_file'):
        result.setdefault('file', result['active_document']['source_file'])
    return result


def _cached_plc_read(request: dict):
    if any(request.get(key) for key in
           ("live", "include_member_code", "member_type", "start_line", "max_lines")):
        return None
    return PLC_SOURCE_CACHE.get(
        request["name"], request.get("method") or "", request.get("area") or "all",
        path=request.get("path") or "",
    )


def _plc_com_read(request: dict) -> object:
    result = ps_com(
        "read-pou", name=request["name"], area=request.get("area") or "all",
        path=request.get("path") or "", method=request.get("method") or "",
        member_type=request.get("member_type") or "",
        include_member_code=bool(request.get("include_member_code", False)),
        start_line=max(1, int(request.get("start_line") or 1)),
        max_lines=max(0, int(request.get("max_lines") or 0)),
    )
    return _annotate_plc_read_identity(result)


def _direct_project(manager: DirectSystemManager, request: dict):
    projects = manager.attach()
    requested = request.get("protocol_project_id", request.get("project_id"))
    if requested is None:
        if len(projects) != 1:
            raise ValueError("direct System Manager read requires protocol_project_id when multiple projects are open")
        requested = projects[0].protocol_id
    return manager.require_project(requested)


def _direct_endpoint_manager():
    discovery = discover_system_manager_endpoint()
    if not discovery.available or discovery.endpoint is None:
        raise EndpointUnavailable(discovery.reason)
    transport = SystemManagerWebSocketTransport(discovery.endpoint.url, timeout_s=30.0)
    return DirectSystemManager(transport, endpoint=discovery.endpoint, timeout_s=30.0), transport


def _plc_direct_read(request: dict) -> dict:
    manager = transport = None
    try:
        manager, transport = _direct_endpoint_manager()
        project = _direct_project(manager, request)
        tname = str(request.get("tname") or request.get("path") or "").strip() or None
        tid = request.get("tid")
        if tname is None and tid is None:
            raise ValueError("direct System Manager read requires tname/path or tid")
        result = manager.read_plc_pou(project, tid=tid, tname=tname).as_dict()
        result.update({
            "name": request["name"],
            "method": request.get("method") or "",
            "path": request.get("path") or tname or "",
            "direct_protocol_project_id": project.protocol_id,
            "direct_endpoint": manager.endpoint.url if manager.endpoint else "",
        })
        return result
    except (EndpointUnavailable, ProtocolError, TimeoutError, OSError, ValueError) as direct_error:
        # Direct access is optional. Keep the established COM path and expose
        # the failed direct identity instead of pretending COM was direct.
        result = dict(_plc_com_read(request))
        result["direct_fallback"] = True
        result["direct_error"] = str(direct_error)
        return result
    finally:
        if manager is not None:
            manager.close()
        if transport is not None:
            transport.close()


def _plc_read(args: dict) -> object:
    if (args.get('structure_baseline') or args.get('document_baseline')) and not args.get('path'):
        if args.get('method') or args.get('direct') or args.get('source_path'):
            raise ValueError('A parent baseline cannot use a method, direct mode or saved-source path')
        # Resolve only a unique live exact-name match. Never guess a folder or
        # use the active editor when a parent path was omitted by the caller.
        found = ps_com('find-pou', query=args['name'])
        matches = found.get('matches', []) if isinstance(found, dict) else []
        exact = [m for m in matches if str(m.get('name','')).casefold()==args['name'].casefold()]
        if (not isinstance(found, dict) or found.get('truncated') or
                found.get('total',len(matches)) != len(matches) or len(exact)!=1 or
                not str(exact[0].get('path','')).startswith('TIPC^')):
            raise ValueError('Parent baseline requires one unique live exact-name match; provide the exact tree_path')
        args = {**args, 'path': exact[0]['path']}
    if args.get('document_baseline'):
        if not args.get('path') or args.get('method') or args.get('direct') or args.get('structure_baseline'):
            raise ValueError('document_baseline requires an exact parent path, no method/direct/structure_baseline')
        return ps_com('document-baseline', name=args['name'], path=args['path'])
    if args.get('structure_baseline'):
        if not args.get('path') or args.get('method') or args.get('direct'):
            raise ValueError('structure_baseline requires an exact parent path, no method/direct')
        return ps_com('member-baseline', name=args['name'], path=args['path'])
    if args.get("source_path") and not args.get("path"):
        raise ValueError(
            "source_path 是已保存索引路径，不能用于实时 COM 读取；"
            "请改用 plc_read_smart(live=false) 或先用 plc_find 获取 COM 树路径"
        )
    request = _plc_read_request(args)
    if request.get("direct"):
        return _plc_direct_read(request)
    cached = _cached_plc_read(request)
    if cached is not None:
        return cached
    return _plc_com_read(request)


def _plc_diagnostics(args: dict) -> dict:
    """Read existing diagnostics directly when explicitly requested.

    This never calls ``sm.build``.  The direct protocol does not expose an
    explicit completeness bit in all XAE versions, so an empty or otherwise
    unmarked response remains incomplete.
    """
    if not args.get("direct"):
        return com_diagnostics()
    manager = transport = None
    try:
        manager, transport = _direct_endpoint_manager()
        project = _direct_project(manager, args)
        tname = str(args.get("tname") or "").strip() or None
        tid = args.get("tid")
        if tname is None and tid is None:
            raise ValueError("direct diagnostics requires tname or tid")
        raw = manager.compiler_messages(project, tid=tid, tname=tname, level=int(args.get("level") or 10))
        result = normalize_system_manager_diagnostics(raw, project_id=project.protocol_id)
        result.update({
            "diagnostics_complete": result.get('complete') is True,
            "diagnostic_scope": "project", "compiler_verified": False, "buildPerformed": False,
            "next_action": "这是指定项目的现存诊断，不证明整个解决方案编译通过；恢复解决方案构建证据请用 plc_diagnostics(direct=false)。",
            "direct_endpoint": manager.endpoint.url if manager.endpoint else "",
            "direct_protocol_project_id": project.protocol_id,
            "tname": tname,
            "tid": tid,
        })
        return result
    except (EndpointUnavailable, ProtocolError, TimeoutError, OSError, ValueError) as direct_error:
        result = dict(com_diagnostics())
        result["direct_fallback"] = True
        result["direct_error"] = str(direct_error)
        return result
    finally:
        if manager is not None:
            manager.close()
        if transport is not None:
            transport.close()


def _plc_direct_batch(args: dict) -> dict:
    """Run an explicitly permissioned direct batch under the backend XAE lock."""
    raw_items = list(args.get("items") or [])
    if not raw_items or len(raw_items) > 100:
        raise ValueError("items must contain 1..100 operations")
    items = [BatchItem(
        str(item.get("item_id") or "").strip(),
        str(item.get("command") or "").strip(),
        dict(item.get("payload") or {}),
        str(item.get("mode") or "read"),
        item.get("expected_revision"),
    ) for item in raw_items]
    if any(not item.item_id or not item.command for item in items):
        raise ValueError("each batch item requires item_id and command")
    write_items = [item for item in items if item.mode == "write"]
    if any(item.command != "sm.plcpou" for item in write_items):
        raise ValueError("direct batch writes are limited to sm.plcpou")
    manager = transport = None
    try:
        manager, transport = _direct_endpoint_manager()
        project = _direct_project(manager, args)
        for item in items:
            item.payload.setdefault("pid", project.protocol_id)

        def preflight(item):
            payload = item.payload
            current = manager.read_plc_pou(
                project, tid=payload.get("tid"), tname=payload.get("tname"),
            ).as_dict()
            revision = hashlib.sha256(json.dumps(
                [current.get("declaration", ""), current.get("implementation", "")],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            return {"revision": revision, **current}

        def readback(item, _reply):
            payload = item.payload
            current = manager.read_plc_pou(
                project, tid=payload.get("tid"), tname=payload.get("tname"),
            ).as_dict()
            for field in ("interface", "implementation"):
                wanted = payload.get(field)
                if wanted is not None:
                    actual = current.get("declaration" if field == "interface" else field, "")
                    if actual != wanted:
                        return False
            return True

        result = BatchExecutor(transport).execute(
            items,
            allow_mutation=bool(args.get("apply")),
            preflight=preflight if write_items else None,
            # Every direct write receives a post-write readback verifier.
            readback=readback if write_items else None,
            cancel=args.get("cancel"),
            timeout_s=float(args.get("timeout_s") or 30.0),
            scope=project.protocol_id,
        ).as_dict()
        result.update({
            "direct_endpoint": manager.endpoint.url if manager.endpoint else "",
            "direct_protocol_project_id": project.protocol_id,
            "write_readback_required": bool(write_items),
        })
        return result
    finally:
        if manager is not None:
            manager.close()
        if transport is not None:
            transport.close()


def _plc_direct_read_batch(args: dict) -> dict:
    raw_requests = list(args.get("requests") or [])
    if not raw_requests or len(raw_requests) > 16:
        raise ValueError("requests must contain 1..16 items")
    manager = transport = None
    try:
        manager, transport = _direct_endpoint_manager()
        project = _direct_project(manager, args)
        items = []
        for index, request in enumerate(raw_requests):
            request = _plc_read_request(dict(request or {}))
            tname = str(request.get("tname") or request.get("path") or "").strip()
            tid = request.get("tid")
            if not tname and tid is None:
                raise ValueError(f"requests[{index}] requires tname/path or tid for direct reading")
            payload = {
                "pid": project.protocol_id, "interface": None, "implementation": None,
            }
            if tname:
                payload["tname"] = tname
            if tid is not None:
                payload["tid"] = tid
            items.append(BatchItem(str(index), "sm.plcpou", payload, "read"))
        batch = BatchExecutor(transport).execute(
            items, timeout_s=float(args.get("timeout_s") or 30.0), scope=project.protocol_id,
        )
        results = []
        for request, item in zip(raw_requests, batch.items):
            response = item.response if isinstance(item.response, dict) else {}
            result = {
                "status": "read" if item.status == "succeeded" else item.status,
                "name": request.get("name", ""),
                "method": request.get("method", ""),
                "path": request.get("path") or request.get("tname") or "",
                "source": "system_manager",
                "live_xae": True,
                "authoritative": True,
                "direct_protocol_project_id": project.protocol_id,
                "declaration": response.get("interface", ""),
                "implementation": response.get("implementation", ""),
            }
            if item.error:
                result["error"] = item.error
            results.append(result)
        return {
            "status": "read" if all(item.status == "succeeded" for item in batch.items) else "partial",
            "results": results,
            "strategy": "system-manager-read-batch",
            "direct_endpoint": manager.endpoint.url if manager.endpoint else "",
            "direct_protocol_project_id": project.protocol_id,
            "batch": batch.as_dict(),
        }
    finally:
        if manager is not None:
            manager.close()
        if transport is not None:
            transport.close()


def _plc_read_fast(args: dict) -> dict:
    """Batch saved-source reads; live=true explicitly selects the COM path."""
    from tc_agent.plc_path_contract import normalize
    args = normalize('plc_read_fast', args)
    raw_requests = list(args.get("requests") or [])
    if not raw_requests or len(raw_requests) > 16:
        raise ValueError("requests 必须包含 1~16 项")
    if args.get("direct"):
        return _plc_direct_read_batch(args)
    requests = []
    for index, raw in enumerate(raw_requests):
        if args.get("live") and isinstance(raw, dict) and raw.get("source_path") and not raw.get("path"):
            raise ValueError(
                f"requests[{index}].source_path 是已保存索引路径，不能用于 live COM 读取；"
                "请先用 plc_find 获取 COM 树路径"
            )
        request = _plc_read_request(dict(raw or {}))
        if not str(request.get("name") or "").strip():
            raise ValueError(f"requests[{index}].name 不能为空")
        request["area"] = str(request.get("area") or "all")
        request["start_line"] = max(1, int(request.get("start_line") or 1))
        request["max_lines"] = min(500, max(1, int(request.get("max_lines") or 120)))
        request["include_member_code"] = False
        requests.append(request)
    max_total_chars = max(
        1000,
        min(int(args.get("max_total_chars") or PLC_BATCH_MAX_TOTAL_CHARS),
            PLC_BATCH_MAX_TOTAL_CHARS),
    )
    live = bool(args.get("live", False))
    if live:
        result = ps_com(
            "read-batch", requests=requests, max_total_chars=max_total_chars,
            timeout=120.0,
        )
        result["strategy"] = "single-helper-single-dte-live-batch"
    else:
        solution = _saved_solution()
        if not solution:
            raise FileNotFoundError("当前 XAE 没有已保存的解决方案路径；实时读取请传 live=true")
        # Preserve request order but eliminate exact duplicates within a batch.
        unique, mapping, seen = [], [], {}
        for request in requests:
            key = (request["name"], request.get("path") or "", request.get("method") or "",
                   request.get("area") or "all")
            if key not in seen:
                seen[key] = len(unique)
                unique.append(request)
            mapping.append(seen[key])
        indexed = read_many_indexed_source(solution, unique)
        result = {"status": "read", "results": [dict(indexed[i]) for i in mapping],
                  "unique_requests": len(unique), "deduplicated_requests": len(requests) - len(unique),
                  "strategy": "single-index-sync-saved-source-batch", "live_xae": False}
    for item in result.get("results") or []:
        _annotate_plc_read_identity(item, saved=not live)
    used = 0
    for index, item in enumerate(result.get("results") or []):
        known = dict(requests[index].get("known_hashes") or {}) if index < len(requests) else {}
        hashes = dict(item.get("hashes") or {})
        unchanged = set(item.get("unchanged_areas") or [])
        for area in ("declaration", "implementation"):
            if area not in item:
                continue
            text = str(item.get(area) or "")
            digest = hashes.get(area) or hashlib.sha256(text.encode("utf-8")).hexdigest()
            hashes[area] = digest
            if str(known.get(area) or "") == digest:
                item[area] = ""
                unchanged.add(area)
                continue
            remaining = max_total_chars - used
            if remaining <= 0:
                item[area] = ""
                item[f"{area}_omitted"] = "batch character budget exhausted"
            elif len(text) > remaining:
                item[area] = text[:remaining]
                item[f"{area}_omitted_chars"] = len(text) - remaining
                used += remaining
            else:
                used += len(text)
        item["hashes"] = hashes
        item["unchanged_areas"] = sorted(unchanged)
    result["total_chars"] = used
    result["max_total_chars"] = max_total_chars
    return result


def _plc_read_smart(args: dict) -> dict:
    """Read disk source quickly, or use COM when an authoritative live view is required."""
    from tc_agent.plc_path_contract import normalize
    args = normalize('plc_read_smart', args)
    request = _plc_read_request(args)
    if request.get("direct"):
        return _plc_direct_read(request)
    live = bool(args.get("live", False))
    started = time.perf_counter()
    cached = _cached_plc_read(request)
    if cached is not None and not live:
        cached["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        cached["hint"] = "来自 XAE 扩展缓存；需要 COM 权威回读时传 live=true"
        return cached
    if live:
        if args.get("source_path") and not args.get("path"):
            raise ValueError(
                "source_path 是已保存索引路径，不能直接用于 live COM 读取；"
                "请先用 plc_find 获取精确 TIPC 树路径"
            )
        # Smart live mode is an authoritative whole-area read.  Do not inherit
        # plc_read's UI-friendly 120-line default, otherwise hashes compare a
        # truncated COM slice with the complete disk XML on large POUs.
        result = ps_com(
            "read-pou", name=request["name"],
            area=request.get("area") or "all",
            path=request.get("path") or "", method=request.get("method") or "",
            member_type=request.get("member_type") or "",
            include_member_code=False, start_line=1, max_lines=0,
        )
        for area in ("declaration", "implementation"):
            if area in result:
                result.setdefault("hashes", {})[area] = hashlib.sha256(
                    str(result.get(area) or "").encode("utf-8")).hexdigest()
        _annotate_plc_read_identity(result)
        result["source"] = "live_com"
        result["live_xae"] = True
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["authoritative"] = True
        return result
    try:
        solution = _saved_solution()
        if not solution:
            raise FileNotFoundError("当前 XAE 没有已保存的解决方案路径")
        result = read_indexed_source(
            solution, request["name"], path=request.get("path") or "",
            member=request.get("method") or "", area=request.get("area") or "all",
        )
        result["disk_elapsed_ms"] = result.get("elapsed_ms")
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["authoritative"] = False
        result["dirty_unknown"] = True
        _annotate_plc_read_identity(result, saved=True)
        result["hint"] = "验证、写后反读或需要未保存编辑时请传 live=true"
        return result
    except (FileNotFoundError, ValueError, OSError, ET.ParseError) as exc:
        if args.get('source_path'):
            raise ValueError('精确索引读取失败，不回退到同名 COM 对象：' + str(exc)) from exc
        result = _plc_read_smart({**args, "live": True})
        result["source"] = "live_com_fallback"
        result["live_xae"] = True
        result["authoritative"] = True
        result["disk_error"] = str(exc)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result


def _plc_source_index_status(args: dict) -> dict:
    """Build/check the saved-source SQLite index for the active solution."""
    solution = str(args.get("solution") or "")
    if not solution:
        solution = _saved_solution()
    if not solution:
        raise FileNotFoundError("当前 XAE 没有已保存的解决方案路径")
    return source_index_status(solution)


def _plc_source_catalog(args: dict) -> dict:
    """Return a compact saved-source project map for planning bulk reads."""
    solution = str(args.get("solution") or "")
    if not solution:
        solution = _saved_solution()
    if not solution:
        raise FileNotFoundError("当前 XAE 没有已保存的解决方案路径")
    return source_index_catalog(
        solution, query=str(args.get("query") or ""), kind=str(args.get("kind") or ""),
        include_members=bool(args.get("include_members", True)),
        limit=min(500, max(1, int(args.get("limit") or 200))),
    )


def _plc_search_indexed(args: dict) -> dict:
    """Search saved source via the project SQLite index; COM remains fallback."""
    solution = _saved_solution()
    if not solution:
        return search_code_result(
            args["pattern"], regex=bool(args.get("regex", False)),
            ignore_case=not bool(args.get("case_sensitive", False)), pou=args.get("pou") or "",
            path=args.get("path") or "", max_results=min(
                500, max(1, int(args.get("max_results") or 50))
            ),
        )
    try:
        return search_indexed_source(
            solution, args["pattern"], regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("case_sensitive", False)), pou=args.get("pou") or "",
            path=args.get("path") or "", max_results=min(
                500, max(1, int(args.get("max_results") or 50))
            ),
        )
    except (FileNotFoundError, OSError, ET.ParseError):
        return search_code_result(
            args["pattern"], regex=bool(args.get("regex", False)),
            ignore_case=not bool(args.get("case_sensitive", False)), pou=args.get("pou") or "",
            path=args.get("path") or "", max_results=min(
                500, max(1, int(args.get("max_results") or 50))
            ),
        )


def _plc_read_current(args: dict) -> dict:
    """Read exactly the XAE document the user is currently editing."""
    started = time.perf_counter()
    result = ps_com("read-current", area=str(args.get("area") or "all"),
                    member_type=str(args.get("member_type") or ""), timeout=120.0)
    for area in ("declaration", "implementation"):
        if area in result:
            result.setdefault("hashes", {})[area] = hashlib.sha256(
                str(result.get(area) or "").encode("utf-8")).hexdigest()
    result.update({"source": "live_com", "live_xae": True, "authoritative": True,
                   "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
    return result


def _plc_dirty_current(args: dict) -> dict:
    """Compare only the current live editor object with its on-disk source file."""
    started = time.perf_counter()
    live = _plc_read_current(args)
    active = dict(live.get("active_document") or {})
    source_file = str(active.get("source_file") or "")
    if not source_file:
        raise RuntimeError("当前活动文档没有可比较的 PLC 源文件路径")
    member = str(active.get("member") or live.get("method") or "")
    disk = read_source_file(source_file, str(live.get("name") or ""),
                            member=member, area=str(args.get("area") or "all"))
    differences = []
    for area in ("declaration", "implementation"):
        if area not in live and area not in disk:
            continue
        live_text = "\n".join(str(live.get(area) or "").splitlines()).rstrip()
        disk_text = "\n".join(str(disk.get(area) or "").splitlines()).rstrip()
        if live_text != disk_text:
            limit = min(len(live_text), len(disk_text))
            index = next((i for i in range(limit) if live_text[i] != disk_text[i]), limit)
            differences.append({"area": area, "live_chars": len(live_text),
                                "disk_chars": len(disk_text), "first_difference": index})
    return {
        "status": "dirty" if differences else "clean",
        "dirty": bool(differences), "saved": active.get("saved"),
        "name": live.get("name"), "method": member, "path": live.get("path"),
        "active_document": active, "differences": differences,
        "current_lookup_strategy": live.get("current_lookup_strategy"),
        "live_hashes": live.get("hashes"), "disk_hashes": disk.get("hashes"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _guarded_plc_create(args: dict) -> dict:
    declaration = str(args.get("declaration") or "")
    from tc_template.creation_source import declaration_for_creation
    declaration = declaration_for_creation(args.get('type', 'fb'), args['name'], declaration,
                                           str(args.get('return_type') or ''))
    implementation = str(args.get("implementation") or "")
    if (_quality_gate_enabled()
            and str(args.get("type") or "fb").casefold() == "fb" and not re.search(
        r"\b(?:EXTENDS|IMPLEMENTS)\b", declaration, re.IGNORECASE
    )):
        return {
            "status": "blocked", "written": False,
            "reason": (
                "标准 FB 不允许通过原始 plc_create 自由生成。先调用 fblib_find；无匹配时"
                "调用 plc_generate，再用 plc_create_standard_fb 创建规则骨架。"
            ),
            "recommended_tools": ["fblib_find", "plc_generate", "plc_create_standard_fb"],
        }
    if declaration or implementation:
        object_type = str(args.get("type") or "fb").casefold()
        folder = "Interfaces" if object_type == "interface" else (
            "DUTs" if object_type in {"struct", "enum", "union", "alias"} else (
                "GVLs" if object_type == "gvl" else "POUs"
            )
        )
        review = review_write_candidate({
            "name": args["name"], "folder": folder,
            "declaration": declaration, "implementation": implementation,
            "methods": [],
        }, changed_area="all")
        review["name"] = args["name"]
        _attach_plc_dependencies(review, {
            'name': args['name'], 'declaration': declaration, 'implementation': implementation,
        }, args.get('path', ''))
        if _quality_review_blocks(review):
            return {"status": "blocked", "written": False, "review": review}
    else:
        review = {"approved": True, "summary": {"total": 0, "by_rule": {}},
                  "blocking_findings": [], "advisories": []}
    result = ps_com("new-pou", name=args["name"], type=args.get("type", "fb"),
                    declaration=declaration, implementation=implementation,
                    return_type=args.get("return_type", ""),
                    path=args.get("path", ""))
    return {"status": "created", "written": True, "result": result, "review": review}


def _guarded_plc_create_member(args: dict) -> dict:
    declaration = str(args.get("declaration") or "")
    implementation = str(args.get("implementation") or "")
    if str(args.get("member_type") or "method").casefold() == "property":
        return {
            "status": "blocked", "written": False,
            "reason": "属性必须用 plc_create_property 一次性创建 Property 与 Get/Set 访问器。",
            "recommended_tool": "plc_create_property",
        }
    review = review_write_candidate({
        "name": f"{args['pou']}.{args['name']}", "folder": "POUs",
        "declaration": declaration, "implementation": implementation, "methods": [],
    }, changed_area="all")
    member_candidate = {'name': args['name'], 'declaration': declaration, 'implementation': implementation}
    member_path = args.get('path', '')
    if implementation:
        parent = ps_com('read-pou', name=args['pou'], path=member_path, area='declaration',
                        include_member_code=False, start_line=1, max_lines=0)
        if 'declaration' not in parent or parent.get('declaration_paging', {}).get('truncated'):
            return {'status': 'blocked', 'written': False, 'error': 'Complete parent declaration is required.'}
        member_candidate['enclosing_declaration'] = parent['declaration']
        member_path = parent.get('path') or member_path
        if args.get('member_type', 'method') == 'method' and not re.search(r'\bMETHOD\b', declaration, re.I):
            member_candidate['declaration'] = 'METHOD ' + args['name'] + ' : ' + str(args.get('return_type') or 'BOOL') + '\n' + declaration
    _attach_plc_dependencies(review, member_candidate, member_path)
    if _quality_review_blocks(review):
        return {"status": "blocked", "written": False, "review": review}
    result = ps_com("new-member", pou=args["pou"], name=args["name"],
                    type=args.get("member_type", "method"),
                    return_type=args.get("return_type", "BOOL"),
                    declaration=declaration, implementation=implementation,
                    path=args.get("path", ""))
    return {"status": "created", "written": True, "result": result, "review": review}


def _git_sync_editor_guard() -> dict | None:
    """Refuse a multi-object sync when the visible PLC editor is dirty."""
    try:
        current = ps_com("read-current", area="all", timeout=120.0)
    except Exception as exc:  # A non-PLC active editor is a normal case.
        message = str(exc).casefold()
        if any(token in message for token in (
                "不是可读取的 twinCAT plc", "not a readable", "no active", "没有活动文档")):
            return None
        return {
            "status": "unknown", "written": False, "not_executed": True,
            "error_type": "source_editor_state_unknown",
            "error": f"无法确认当前 XAE 编辑器状态: {exc}",
            "next_action": "先用 plc_read_current 或 plc_dirty_current 确认活动 PLC 文档状态；不要在未知状态下同步。",
        }
    active = current.get("active_document") or {}
    if active.get("saved") is False:
        return {
            "status": "conflict", "written": False, "not_executed": True,
            "error_type": "plc_editor_dirty",
            "error": "当前 XAE PLC 编辑器存在未保存修改，拒绝 Git 同步以免覆盖编辑器缓冲区。",
            "active_document": active,
            "next_action": "先保存或处理当前编辑器内容，再重新执行 plc_git_sync；Agent 不自动保存或丢弃。",
        }
    return None


def _git_sync_inventory() -> list[dict]:
    inventory = ps_com("list")
    if not isinstance(inventory, list):
        raise ValueError("XAE PLC 对象清单格式无效")
    return [item for item in inventory if isinstance(item, dict)]


def _git_sync_paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _git_sync_repository_scope_guard(repo_dir: str) -> dict | None:
    """Keep Git's working tree outside the open XAE solution directory."""
    context = ps_com("connect-check")
    solution = str(context.get("solution") or "") if isinstance(context, dict) else ""
    if not solution:
        return None
    solution_path = Path(solution).expanduser().resolve()
    repo_path = Path(repo_dir).expanduser().resolve()
    if not _git_sync_paths_overlap(repo_path, solution_path.parent):
        return None
    return {
        "status": "blocked", "written": False, "not_executed": True,
        "error_type": "git_xae_path_conflict",
        "error": "Git 工作目录不能与当前打开的 XAE 解决方案目录重叠；禁止在 XAE 打开时直接覆盖工程 XML。",
        "repository": str(repo_path), "solution": str(solution_path),
        "next_action": "把 GitHub 仓库 clone 到独立目录，再由 plc_git_sync 通过 COM 同步到当前 XAE。",
    }


def _git_sync_target(source, inventory: list[dict], object_paths: dict[str, str]) -> tuple[dict | None, str | None]:
    """Resolve one source by exact object name, never by a fuzzy name match."""
    explicit = str(object_paths.get(source.name) or "").strip()
    if explicit:
        matches = [item for item in inventory
                   if str(item.get("path") or "").casefold() == explicit.casefold()]
        if len(matches) != 1:
            return None, f"object_paths['{source.name}'] 不是当前 XAE 的唯一对象路径: {explicit}"
        if str(matches[0].get("name") or "").casefold() != source.name.casefold():
            return None, f"对象路径 {explicit} 的名称与 Git 源对象 {source.name} 不一致"
        return matches[0], None
    matches = [item for item in inventory
               if str(item.get("name") or "").casefold() == source.name.casefold()]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        choices = ", ".join(str(item.get("path") or "") for item in matches[:8])
        return None, f"对象名 {source.name} 在当前 XAE 中有多个匹配，请提供 object_paths: {choices}"
    return None, None


def _git_sync_expected_item_type(create_type: str) -> int:
    return {
        "program": 602, "function": 603, "fb": 604,
        "enum": 605, "struct": 606, "union": 607,
        "alias": 623, "gvl": 615, "interface": 618,
    }.get(str(create_type or "").casefold(), 0)


def _git_sync_read_member(name: str, path: str, member: str) -> dict:
    return ps_com("read-pou", name=name, path=path, method=member,
                  area="all", include_member_code=False,
                  start_line=1, max_lines=0)


def _git_sync_member_plan(source, target: dict, create_missing: bool) -> tuple[list[dict], list[str]]:
    """Plan top-level methods/properties and their accessors."""
    operations: list[dict] = []
    blockers: list[str] = []
    direct = [member for member in source.members if "." not in member.path]
    by_parent: dict[str, list] = {}
    for member in source.members:
        if "." in member.path:
            by_parent.setdefault(member.path.split(".", 1)[0].casefold(), []).append(member)

    for member in direct:
        try:
            current = _git_sync_read_member(source.name, target["path"], member.path)
            operations.append({"path": member.path, "action": "update", "source": member})
        except Exception as exc:
            message = str(exc)
            if not create_missing:
                blockers.append(f"{source.name}.{member.path} 不存在: {message}")
                continue
            if member.kind not in {"method", "action", "transition", "property"}:
                blockers.append(f"{source.name}.{member.path} 类型 {member.kind} 不支持自动创建")
                continue
            operations.append({"path": member.path, "action": "create", "source": member})
            current = None

        if member.kind == "property":
            children = by_parent.get(member.path.casefold(), [])
            for child in children:
                try:
                    _git_sync_read_member(source.name, target["path"], child.path)
                    operations.append({"path": child.path, "action": "update", "source": child})
                except Exception as exc:
                    if not create_missing or child.kind not in {"get", "set"}:
                        blockers.append(f"{source.name}.{child.path} 不存在: {exc}")
                    else:
                        operations.append({"path": child.path, "action": "create", "source": child})
    return operations, blockers


def _git_sync_plan_entry(source, target: dict | None, target_error: str | None,
                         create_missing: bool) -> tuple[dict, list[str]]:
    from tc_agent.plc_git_sync import equivalent

    blockers: list[str] = []
    if target_error:
        blockers.append(target_error)
    if target is None:
        entry = {
            "source": source.relative_file, "name": source.name,
            "kind": source.kind, "action": "create" if create_missing else "missing",
            "target_path": "", "changes": ["create"],
            "member_count": len(source.members),
        }
        if not create_missing:
            blockers.append(f"当前 XAE 不存在对象 {source.name}；设置 create_missing=true 才允许创建")
        return entry, blockers

    try:
        current = ps_com("read-pou", name=source.name, path=target.get("path") or "",
                         area="all", include_member_code=False,
                         start_line=1, max_lines=0)
    except Exception as exc:
        blockers.append(f"无法读取当前 XAE 对象 {source.name}: {exc}")
        return {
            "source": source.relative_file, "name": source.name,
            "kind": source.kind, "action": "blocked",
            "target_path": target.get("path") or "", "changes": [],
        }, blockers

    expected_type = _git_sync_expected_item_type(source.create_type)
    actual_type = int(current.get("itemType") or target.get("itemType") or 0)
    if expected_type and actual_type and expected_type != actual_type:
        blockers.append(
            f"对象 {source.name} 类型不一致：Git={source.create_type}/{expected_type}，"
            f"XAE={actual_type}；拒绝把不同类型对象互相覆盖"
        )
    changes: list[str] = []
    if not equivalent(source.declaration, current.get("declaration") or ""):
        changes.append("declaration")
    if source.kind == "pou" and not equivalent(source.implementation, current.get("implementation") or ""):
        changes.append("implementation")
    member_ops, member_blockers = _git_sync_member_plan(
        source, {"path": target.get("path") or ""}, create_missing)
    blockers.extend(member_blockers)
    return {
        "source": source.relative_file, "name": source.name,
        "kind": source.kind, "action": "update",
        "target_path": target.get("path") or "", "changes": changes,
        "member_changes": [item["path"] for item in member_ops],
        "member_count": len(source.members),
    }, blockers


def _git_sync_apply_member(source, target_path: str, member, action: str,
                           property_children: dict[str, list]) -> dict:
    from tc_agent.plc_git_sync import equivalent

    if action == "create":
        if member.kind == "property":
            children = property_children.get(member.path.casefold(), [])
            getter = next((item for item in children if item.kind == "get"), None)
            setter = next((item for item in children if item.kind == "set"), None)
            return _create_property({
                "pou": source.name, "path": target_path, "name": member.name,
                "return_type": member.return_type or "BOOL",
                "getter_implementation": getter.implementation if getter else "",
                "setter_implementation": setter.implementation if setter else "",
            })
        member_type = {"method": "method", "action": "action", "transition": "transition"}.get(member.kind)
        if not member_type:
            return {"status": "blocked", "written": False,
                    "error": f"不支持自动创建成员类型: {member.kind}"}
        return _guarded_plc_create_member({
            "pou": source.name, "path": target_path, "name": member.name,
            "member_type": member_type, "return_type": member.return_type or "BOOL",
            "declaration": member.declaration, "implementation": member.implementation,
        })

    results = []
    if member.declaration:
        live = _git_sync_read_member(source.name, target_path, member.path)
        if not equivalent(member.declaration, live.get("declaration") or ""):
            results.append(_guarded_plc_write({
                "name": source.name, "path": target_path, "method": member.path,
                "area": "declaration", "code": member.declaration,
            }))
    if member.implementation:
        live = _git_sync_read_member(source.name, target_path, member.path)
        if not equivalent(member.implementation, live.get("implementation") or ""):
            results.append(_guarded_plc_write({
                "name": source.name, "path": target_path, "method": member.path,
                "area": "implementation", "code": member.implementation,
            }))
    failed = [item for item in results if not tool_succeeded(item)]
    return {"status": "blocked" if failed else "unchanged" if not results else "updated",
            "written": bool(results) and not failed, "verified": not failed,
            "member": member.path, "results": results}


def _git_sync_apply_source(source, target_path: str, create: bool) -> dict:
    from tc_agent.plc_git_sync import equivalent

    if create:
        created = _guarded_plc_create({
            "name": source.name, "type": source.create_type,
            "declaration": source.declaration, "implementation": source.implementation,
            "return_type": source.return_type,
        })
        if not tool_succeeded(created):
            return {"status": "blocked", "written": False, "stage": "create", "result": created}
        inventory = _git_sync_inventory()
        target, error = _git_sync_target(source, inventory, {})
        if error or target is None:
            return {"status": "uncertain", "written": True, "verified": False,
                    "stage": "resolve_created_object", "error": error or "创建后无法定位对象"}
        target_path = str(target.get("path") or "")

    current = ps_com("read-pou", name=source.name, path=target_path,
                     area="all", include_member_code=False,
                     start_line=1, max_lines=0)
    changed_decl = not equivalent(source.declaration, current.get("declaration") or "")
    changed_impl = source.kind == "pou" and not equivalent(
        source.implementation, current.get("implementation") or "")
    results = []
    if changed_decl and changed_impl and source.kind == "pou":
        results.append(_guarded_plc_write({
            "name": source.name, "path": target_path, "area": "declaration",
            "code": source.declaration, "implementation": source.implementation,
        }))
    else:
        if changed_decl:
            results.append(_guarded_plc_write({
                "name": source.name, "path": target_path, "area": "declaration",
                "code": source.declaration,
            }))
        if changed_impl:
            results.append(_guarded_plc_write({
                "name": source.name, "path": target_path, "area": "implementation",
                "code": source.implementation,
            }))

    property_children: dict[str, list] = {}
    for member in source.members:
        if "." in member.path:
            property_children.setdefault(member.path.split(".", 1)[0].casefold(), []).append(member)
    direct = [member for member in source.members if "." not in member.path]
    for member in direct:
        try:
            _git_sync_read_member(source.name, target_path, member.path)
            action = "update"
        except Exception:
            action = "create"
        results.append(_git_sync_apply_member(
            source, target_path, member, action, property_children))
        if member.kind == "property":
            for child in property_children.get(member.path.casefold(), []):
                try:
                    _git_sync_read_member(source.name, target_path, child.path)
                    child_action = "update"
                except Exception:
                    child_action = "create"
                # A newly created property already created its accessors.
                if action == "create":
                    continue
                results.append(_git_sync_apply_member(
                    source, target_path, child, child_action, property_children))
    wrote = any(item.get("written") is True for item in results if isinstance(item, dict))
    failed = [item for item in results if isinstance(item, dict) and not tool_succeeded(item)]
    return {
        "status": "updated" if wrote and not failed else "unchanged" if not results else "blocked",
        "written": wrote, "verified": not failed,
        "target_path": target_path, "results": results,
    }


def _plc_git_sync(args: dict) -> dict:
    """Pull a Git checkout and write its native PLC sources into live XAE."""
    from tc_agent.plc_git_sync import discover_sources, is_remote_repository, prepare_repository

    apply = bool(args.get("apply", False))
    repository = str(args.get("repository") or "")
    requested_repo_path = (
        str(args.get("local_path") or "") if is_remote_repository(repository) else repository
    )
    if requested_repo_path:
        scope_guard = _git_sync_repository_scope_guard(requested_repo_path)
        if scope_guard is not None:
            return scope_guard
    prepared = prepare_repository(
        repository, local_path=str(args.get("local_path") or ""),
        branch=str(args.get("branch") or ""), apply=apply,
    )
    if prepared.get("status") == "preview_requires_clone":
        return {**prepared, "plan": [], "next_action": "apply=true 时 clone 后再同步当前 XAE PLC 项目。"}
    sources, parse_errors = discover_sources(
        prepared["local_path"], source_subdir=str(args.get("source_subdir") or ""),
        objects=list(args.get("objects") or []),
    )
    inventory = _git_sync_inventory()
    object_paths = {str(k): str(v) for k, v in (args.get("object_paths") or {}).items()}
    plan: list[dict] = []
    internal: list[tuple[Any, dict | None, bool]] = []
    blockers = list(parse_errors)
    for source in sources:
        target, target_error = _git_sync_target(source, inventory, object_paths)
        entry, errors = _git_sync_plan_entry(
            source, target, target_error, bool(args.get("create_missing", False)))
        plan.append(entry)
        blockers.extend({"file": source.relative_file, "error": error} for error in errors)
        internal.append((source, target, target is None))

    result = {
        "status": "preview" if not apply else "planned",
        "repository": prepared, "source_count": len(sources),
        "plan": plan, "parse_errors": parse_errors,
        "blockers": blockers, "applied": False, "written": False,
        "xae_reopen_performed": False, "xae_close_performed": False,
    }
    if not apply:
        return result
    if blockers:
        return {**result, "status": "blocked", "not_executed": True,
                "next_action": "先处理 blockers；本次未向 XAE 写入任何 PLC 对象。"}
    guard = _git_sync_editor_guard()
    if guard is not None:
        return {**result, **guard}

    applied_results = []
    for source, target, missing in internal:
        applied_results.append(_git_sync_apply_source(
            source, str((target or {}).get("path") or ""),
            missing and bool(args.get("create_missing", False))))
    failed = [item for item in applied_results if not tool_succeeded(item)]
    wrote = any(item.get("written") is True for item in applied_results)
    return {**result, "status": "applied" if not failed else "partial",
            "applied": True, "written": wrote, "verified": not failed,
            "results": applied_results,
            "next_action": "执行 plc_build 验证编译；本工具不自动登录、下载或启动 PLC。"}


def _create_property(args: dict) -> dict:
    pou = str(args["pou"])
    name = str(args["name"])
    path = str(args.get("path") or "")
    getter = str(args.get("getter_implementation") or "")
    setter = str(args.get("setter_implementation") or "")
    enclosing = ''
    if getter or setter:
        parent = ps_com('read-pou', name=pou, path=path, area='declaration',
                        include_member_code=False, start_line=1, max_lines=0)
        if 'declaration' not in parent or parent.get('declaration_paging', {}).get('truncated'):
            return {'status': 'blocked', 'written': False, 'error': 'Complete parent declaration is required before property writes.'}
        enclosing = parent['declaration']
        path = parent.get('path') or path
    reviews = []
    for accessor, implementation in (("Get", getter), ("Set", setter)):
        if not implementation:
            continue
        review = review_write_candidate({
            "name": f"{pou}.{name}.{accessor}", "folder": "POUs",
            "declaration": "", "implementation": implementation, "methods": [],
        }, changed_area="implementation")
        _attach_plc_dependencies(review, {
            'name': f'{pou}.{name}.{accessor}',
            'declaration': 'PROPERTY ' + name + ' : ' + str(args.get('return_type') or 'BOOL'),
            'enclosing_declaration': enclosing, 'implementation': implementation,
        }, path)
        if _quality_review_blocks(review):
            return {"status": "blocked", "written": False, "accessor": accessor,
                    "review": review}
        reviews.append({"accessor": accessor, "review": review})

    created_property = ps_com(
        "new-member", pou=pou, name=name, type="property",
        return_type=args.get("return_type") or "BOOL",
        declaration="", implementation="", path=path,
    )
    try:
        structure = ps_com(
            "read-pou", name=pou, path=path, area="members",
            include_member_code=False,
        )
        property_info = next(
            (item for item in structure.get("methods", []) if item.get("name") == name),
            {},
        )
        existing = {item.get("name") for item in property_info.get("members", [])}
        accessors = []
        for member_type, accessor_name, implementation in (
            ("propget", "Get", getter), ("propset", "Set", setter),
        ):
            if not implementation:
                continue
            if accessor_name in existing:
                result = ps_com(
                    "write-pou", name=pou, path=path,
                    method=f"{name}.{accessor_name}", area="implementation",
                    code=implementation,
                )
            else:
                result = ps_com(
                    "new-member", pou=pou, name=name, type=member_type,
                    return_type=args.get("return_type") or "BOOL",
                    declaration="", implementation=implementation, path=path,
                )
            accessors.append({"name": accessor_name, "result": result})
        return {
            "status": "created", "written": True, "pou": pou,
            "property": name, "property_result": created_property,
            "accessors": accessors, "reviews": reviews,
        }
    except Exception:
        try:
            ps_com(
                "delete-member", pou=pou, name=name, type="property", path=path,
                dry_run=False, force=True,
            )
        except Exception:
            pass
        raise


def _active_solution() -> str:
    info = ps_com("connect-check")
    return str(info.get("solution") or "") if isinstance(info, dict) else ""


def _coding_profile_read(_args: dict) -> dict:
    return coding_profile.load_profile(_active_solution())


def _coding_profile_set(args: dict) -> dict:
    updates = {
        key: args[key]
        for key in (
            "author", "identifier_language", "comment_language", "indent_spaces",
            "require_fb_header", "require_case_state_machine",
            "require_transaction_timeout", "error_code_source", "template_first",
            "extra_rules",
        )
        if key in args
    }
    return coding_profile.save_profile(_active_solution(), updates)


def _generate_standard_fb(args: dict) -> dict:
    from tc_template.fb_scaffold import generate_fb

    profile = coding_profile.load_profile(_active_solution())
    generated = generate_fb(
        args["name"],
        author=args.get("author") or str(profile.get("author") or ""),
        purpose=args.get("purpose") or "",
        mode=args.get("mode") or "transaction",
    )
    indent = min(8, max(2, int(profile.get("indent_spaces") or 4)))
    if indent != 4:
        for key in ("fb_declaration", "fb_implementation", "enum_declaration"):
            lines = []
            for line in str(generated.get(key) or "").splitlines():
                leading = len(line) - len(line.lstrip(" "))
                levels, remainder = divmod(leading, 4)
                lines.append(" " * (levels * indent + remainder) + line.lstrip(" "))
            generated[key] = "\n".join(lines)
    generated["coding_profile"] = {
        "path": profile.get("path", ""), "indent_spaces": indent,
        "comment_language": profile.get("comment_language"),
        "extra_rules": profile.get("extra_rules") or [],
    }
    return generated


def _create_standard_fb(args: dict) -> dict:
    generated = _generate_standard_fb(args)
    created: dict = {"status": "created", "mode": generated["mode"]}
    parent_path = str(args.get("path") or "")
    if generated.get("enum_name") and bool(args.get("with_enum", True)):
        # Explicit discovery has a valid empty result for a missing enum. Do
        # not infer absence from a bridge exception (COM errors can mean lost
        # connection, ambiguous object or permission failure).
        discovery = ps_com("find-pou", query=generated["enum_name"], include_members=False, limit=200)
        if (not isinstance(discovery, dict) or not isinstance(discovery.get("matches"), list)
                or discovery.get("error") or discovery.get("ok") is False):
            raise ValueError("枚举发现结果无效，未创建任何对象")
        matches = [m for m in discovery['matches']
                   if str(m.get('name', '')).casefold() == generated['enum_name'].casefold()
                   and not m.get('member')]
        if len(matches) > 1:
            raise ValueError("存在多个同名状态枚举，请先明确 PLC 项目与路径")
        existing_enum = matches[0] if matches else None
        if existing_enum is not None:
            if int(existing_enum.get("itemType") or 0) != 605:
                raise ValueError(
                    f"Existing object '{generated['enum_name']}' is not an Enum DUT"
                )
            created["enum"] = {
                "name": generated["enum_name"], "status": "existing", "itemType": 605,
            }
        else:
            created["enum"] = ps_com(
                "new-pou", name=generated["enum_name"], type="enum",
                declaration=generated["enum_declaration"], implementation="",
                path=parent_path,
            )
    created["fb"] = ps_com(
        "new-pou", name=generated["fb_name"], type="fb",
        declaration=generated["fb_declaration"],
        implementation=generated["fb_implementation"],
        path=parent_path,
    )
    return created


def _fblib_find(args: dict) -> dict:
    from tc_template.fblib import find_fbs

    return find_fbs(args["intent"], limit=min(10, max(1, int(args.get("limit") or 5))))


def _fblib_add(args: dict) -> dict:
    from tc_template.fblib import add_fb, render_fb

    # Validate the template identity before any COM mutation. Slugs are local
    # catalog identifiers, not arbitrary filesystem paths.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', str(args.get('slug') or '')):
        raise ValueError('Invalid template slug; use the identifier returned by fblib_find')
    rendered = render_fb(args['slug'], params=dict(args.get('params') or {}))
    names = [o.get('name', '') for o in rendered.get('objects', [])] or [rendered.get('name', '')]
    if any(not re.fullmatch(r'[A-Za-z_]\w*', name) for name in names):
        raise ValueError('Template rendered an invalid PLC object name')
    projects = ps_com('plc-runtimes')
    if not isinstance(projects, dict) or not isinstance(projects.get('plcs'), list) or len(projects['plcs']) != 1:
        return {'status': 'blocked', 'not_executed': True, 'written': False,
                'error_type': 'template_scope',
                'error': '模板导入尚不支持多 PLC 目标选择；必须能确认当前解决方案恰有一个 PLC 项目。',
                'next_action': '确认目标工程范围；不要默认选择第一个 PLC 或转为手写。'}

    return add_fb(
        args["slug"], params=dict(args.get("params") or {}),
        add_libraries=bool(args.get("add_libraries", True)), run_lint=True,
    )


def _create_agent_report(args: dict) -> dict:
    connection: dict = {}
    project_info: dict = {}
    try:
        connection = ps_com("connect-check")
    except Exception as exc:
        connection = {"connected": False, "error": str(exc)}
    try:
        project_info = ps_com("project-info")
    except Exception as exc:
        project_info = {"error": str(exc)}
    solution = str(connection.get("solution") or project_info.get("solution") or "")
    try:
        version = (PROJECT_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    return reporting.create_report(
        project_dir=PROJECT_DIR, agent_version=version, solution=solution,
        conversation=active_conversation_snapshot(),
        xae={"connection": connection, "project_info": project_info}, args=args,
    )


REGISTRY: list[dict] = [
    # ---- 项目多对话 / 子任务通讯 ----
    _tool("thread_list", "列出当前 TwinCAT 项目中的对话和子任务，返回当前对话 ID、标题和状态。",
          _OBJ(), True, lambda a: tool_thread_list(), "协作"),
    _tool("thread_inbox", "读取当前对话或指定子对话收到的结构化任务消息。",
          _OBJ({"thread_id": {**_STR, "description": "可选目标对话 ID；留空读取当前对话"},
                "unread_only": {"type": "boolean", "description": "是否只看未读消息"}}),
          True, tool_thread_inbox, "协作"),
    _tool("thread_send", "向项目中的另一个对话发送结构化任务或成果消息；不会直接执行 XAE 写操作。",
          _OBJ({"to_thread": {**_STR, "description": "目标对话 ID"},
                "message_type": {"type": "string", "enum": ["task", "task_result", "context", "notice"]},
                "objective": {**_STR, "description": "任务目标或结果摘要"},
                "context": {"type": "object", "additionalProperties": True},
                "constraints": {"type": "array", "items": _STR},
                "expected_output": _STR},
               ["to_thread", "objective"]),
          False, tool_thread_send, "协作"),
    _tool("thread_rename", "重命名当前对话或当前对话的直属子任务。",
          _OBJ({"thread_id": {**_STR, "description": "可选；默认当前对话"},
                "title": {**_STR, "description": "新标题"}}, ["title"]),
          False, tool_thread_rename, "协作"),
    _tool("thread_fork", "从当前对话分叉一个独立对话，复制模型上下文与摘要但不复制中断状态。",
          _OBJ({"title": {**_STR, "description": "可选分叉标题"}}),
          False, tool_thread_fork, "协作"),
    _tool("memory_list", "列出当前 TwinCAT 项目跨对话共享的已确认事实、决策、偏好和约束。",
          _OBJ({"limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
          True, tool_memory_list, "记忆"),
    _tool("memory_remember", "保存一条结构化项目共享记忆。不得保存 API Key、授权码、密码或令牌；冲突时不会覆盖旧记录。",
          _OBJ({"memory_key": {**_STR, "description": "稳定且具体的记忆键"},
                "category": {"type": "string", "enum": ["fact", "decision", "preference", "constraint"]},
                "content": {**_STR, "description": "简洁、可验证的记忆内容"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
               ["memory_key", "category", "content"]),
          False, tool_memory_remember, "记忆"),
    _tool("memory_forget", "停用一条项目共享记忆，保留审计记录但不再注入上下文。",
          _OBJ({"memory_id": {**_STR, "description": "记忆 ID"}}, ["memory_id"]),
          False, tool_memory_forget, "记忆"),
    _tool("project_data_health", "检查当前项目对话数据库完整性、版本、大小和各类记录数量。",
          _OBJ(), True, lambda a: tool_project_data_health(), "协作"),
    _tool("project_data_backup", "为当前项目的多对话数据库创建一致性 SQLite 快照。",
          _OBJ(), False, lambda a: tool_project_data_backup(), "协作"),
    _tool("project_data_export", "把当前项目的对话、任务、提案、用量与记忆导出为可审计 JSON。导出包含对话正文。",
          _OBJ(), False, lambda a: tool_project_data_export(), "协作"),
    _tool("agent_report_create", "输出可交给开发者分析的脱敏 Bug/建议诊断包（ZIP，含 Markdown+JSON）。默认不包含 PLC 代码正文、API Key、Token 或授权码；自动附带版本、XAE项目状态、最近工具错误和后台日志尾部。",
          _OBJ({
              "report_type": {"type": "string", "enum": ["bug", "suggestion"]},
              "title": {**_STR, "description": "简短标题"},
              "description": {**_STR, "description": "问题现象或建议背景"},
              "steps": {"type": "array", "items": _STR,
                        "description": "Bug复现步骤；建议报告可留空"},
              "expected": {**_STR, "description": "Bug预期结果"},
              "actual": {**_STR, "description": "Bug实际结果"},
              "proposal": {**_STR, "description": "建议方案"},
              "benefit": {**_STR, "description": "建议收益"},
          }, ["report_type", "title", "description"]),
          False, _create_agent_report, "诊断"),
    # ---- 只读:环境 / 代码 ----
    _tool("tc_connect_check", "确认能否连上正在运行的 TwinCAT XAE，返回当前打开的解决方案路径。",
          _OBJ(), True, lambda a: ps_com("connect-check"), "环境"),
    _tool("tc_platform_list", "列出构建平台；切换时原样使用 full，不猜目标架构。",
          _OBJ({}), True, lambda a: ps_com('platform-list'), "平台"),
    _tool("tc_platform_show", "读取当前构建平台及项目映射。",
          _OBJ({}), True, lambda a: ps_com('platform-show'), "平台"),
    _tool("tc_platform_set", "切换构建平台。默认预览，确认后 apply=true 执行并回读；不编译、登录或部署。",
          _OBJ({'full': _STR, 'apply': {'type': 'boolean', 'default': False},
                'acknowledge_target_platform': {'type': 'boolean', 'description': '仅用户确认该平台适合实际目标后为 true，禁止自行猜测'},
                'allow_configuration_change': {'type': 'boolean', 'description': '用户明确要求改变 Debug/Release 才为 true'}}),
          False, lambda a: com_select_build_platform(a.get('full', ''), bool(a.get('apply', False)),
                    bool(a.get('acknowledge_target_platform', False)), bool(a.get('allow_configuration_change', False))), "平台", "system"),
    _tool("tc_state", "读取 TwinCAT System Service 的实际 ADS 状态（Config/Run/停止），TIRS 仅作备用。",
          _OBJ(), True, lambda a: ps_com("state"), "环境"),
    _tool("tc_projects", "列出当前解决方案项目和 TIPC 下的 PLC 项目。",
          _OBJ(), True, lambda a: ps_com("projects"), "项目"),
    _tool("tc_project_info", "读取当前解决方案路径、项目数量、目标 NetId 和 PLC 项目。",
          _OBJ(), True, lambda a: ps_com("project-info"), "项目"),
    _tool("tc_hmi_create_project",
          "使用本机官方 TE2000 模板预览或创建 TwinCAT HMI 工程，并回读确认 XAE 加载和解决方案登记。"
          "同一路径已加载的有效项目会接续保存，不重复创建；incomplete 时按 phase/state/next_action 处理，不删除现有目录。",
          _OBJ({
              "name": {**_STR, "description": "HMI 工程名（英文标识符）"},
              "output_directory": {**_STR, "description": "可选目标目录；默认位于解决方案目录下"},
              "template": {**_STR, "description": "可选本机 .vstemplate；默认官方 Starter 模板"},
              "apply": {"type": "boolean", "description": "false=预览，true=创建"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-create-project", name=a["name"],
                           output_directory=str(a.get("output_directory") or ""),
                           template=str(a.get("template") or ""),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_project_info",
          "读取当前 XAE 中 TwinCAT HMI 项目的工程路径、Framework/Engineering 版本、启动页面、主题和版本号。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径；省略时自动选择唯一项目"}}),
          True, lambda a: ps_com("hmi-project-info", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_structure",
          "清点 TwinCAT HMI 项目的 View、Content、JavaScript、主题、资源及 Server 配置文件；不修改项目。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-structure", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_source_index",
          "增量同步当前解决方案已登记 HMI 文件及控件的 SQLite 索引；不扫描磁盘、不保存编辑器、不连接 PLC。refresh 强制刷新；dirty_unknown=true 表示未保存内容未知。",
          _OBJ({"project": _STR, "refresh": {"type": "boolean"}}), True,
          lambda a: hmi_source.source_index(**a), "HMI"),
    _tool("tc_hmi_source_catalog",
          "优先用本地索引清点 HMI 文件或控件。kind=files/controls，支持 file 精确筛选、query 搜索和 next_offset 分页；complete 表示索引完整性，不代表在线验证。",
          _OBJ({"project": _STR, "kind": {"type": "string", "enum": ["files", "controls"]},
                "file": _STR, "query": _STR, "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), True,
          lambda a: hmi_source.source_catalog(**a), "HMI"),
    _tool("tc_hmi_read_smart",
          "HMI 已保存程序首选读取：只刷新目标文件索引，无需重复 COM 读取。auto 对页面返回控件、脚本/配置返回源码；已知 ID 用 control_id；area=events/bindings 精读事件/绑定，source 分页源码。严格沿用 next_control_offset/next_content_offset；未保存内容未知，refresh=true 强制重读磁盘。",
          _OBJ({"file": _STR, "project": _STR, "control_id": _STR,
                "area": {"type": "string", "enum": ["auto", "controls", "source", "events", "bindings"]},
                "include_content": {"type": "boolean"}, "refresh": {"type": "boolean"},
                "control_offset": {"type": "integer", "minimum": 0},
                "content_offset": {"type": "integer", "minimum": 0},
                "max_controls": {"type": "integer", "minimum": 1, "maximum": 5000},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 1000000}}, ["file"]), True,
          lambda a: hmi_source.read_smart(**a), "HMI"),
    _tool("tc_hmi_read",
          "读取已保存的 HMI 文件，不代表 XAE 未保存内容。已知 ID 用 control_id + include_content=false；清单与源码可分别读取，未读完按返回 next_control_offset/next_content_offset 续读，不反复扩大整页上限。",
          _OBJ({
              "file": {**_STR, "description": "项目内相对路径，例如 Desktop.view 或 Server/ADS/TcHmiSrv.Config.json"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "max_chars": {"type": "integer", "minimum": 1, "maximum": 1000000,
                            "description": "原始文件正文上限；默认 12000，不能用增大它代替控件筛选"},
              "control_id": {**_STR, "description": "精确控件 ID；填写后只返回该控件及其绑定"},
              "include_content": {"type": "boolean", "description": "是否返回原始文件正文；精确控件读取默认 false"},
              "max_controls": {"type": "integer", "minimum": 1, "maximum": 5000,
                               "description": "未指定 control_id 时最多返回的控件数；默认 40"},
              "control_offset": {"type": "integer", "minimum": 0, "description": "控件分页起点，直接沿用返回的 next_control_offset"},
              "content_offset": {"type": "integer", "minimum": 0, "description": "源码分页起点（UTF-16），直接沿用 next_content_offset，勿自行按字符数估算"},
          }, ["file"]), True,
          lambda a: ps_com("hmi-read", project=str(a.get("project") or ""), file=a["file"],
                           max_chars=max(1, min(int(a.get("max_chars") or 12000), 1000000)),
                           control_id=str(a.get("control_id") or ""),
                           include_content=bool(a.get("include_content", not bool(a.get("control_id")))),
                           max_controls=max(1, min(int(a.get("max_controls") or 40), 5000)),
                           control_offset=max(0, int(a.get("control_offset") or 0)),
                           content_offset=max(0, int(a.get("content_offset") or 0))), "HMI"),
    _tool("tc_hmi_write_markup",
          "整页写入并回读。Schema失败若返回candidate_id，保留原稿，仅提交candidate_id和replacements修正错误，"
          "不要重新生成整页。每项old/new字符串和精确count(默认1)。markup与candidate_id互斥。"
          "候选稿30分钟内有效、绑定XAE和原文件指纹，仍执行完整Schema门禁。"
          "禁止因为 tc_hmi_control_edit/tc_hmi_controls_batch 参数失败而改走整页覆写；"
          "raw markup 仅在用户明确要求整体替换页面时允许，并需显式确认。",
          _OBJ({"file": _STR, "markup": _STR, "project": _STR, "candidate_id": _STR,
                "replacements": {"type": "array", "items": _OBJ({"old": _STR, "new": _STR,
                    "count": {"type": "integer", "minimum": 1}}, ["old", "new"])},
                "acknowledge_full_rewrite": {"type": "boolean",
                    "description": "仅用户明确要求整体替换已有页面时设为 true；候选稿精确修复不需要"},
                "full_rewrite_reason": {**_STR,
                    "description": "整体替换的用户需求依据；不能填写‘专用工具失败’"},
                "apply": {"type": "boolean"}}, ["file"]), False,
          _run_hmi_write_markup, "HMI"),
    _tool("tc_hmi_ads_info",
          "只读解析 TwinCAT HMI Server 的默认/远程 ADS Runtime 定义、AMS NetId、端口和启用状态；不连接 PLC。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-ads-info", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_validate",
          "校验 HMI 工程结构和已安装版本的控件/属性 Schema；检查启动页、重复 ID、ADS JSON，返回 schema_findings。不会把结构检查当作浏览器或 ADS 在线验证。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-validate", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_item_create",
          "通过倍福Beckhoff.TcHmi.1.12专用接口AddFolder/AddItem创建原生HMI项目项；javascript_classic/function_js_classic使用AddExistingItem，避免被标记为JavascriptModule，并保留Function描述文件DependentUpon。默认预览。支持folder/view/content/usercontrol/codebehind_js/javascript/javascript_classic/css/function_js/function_js_classic，folder必须已存在，name不含扩展名。parent_file不支持，层级由官方模板生成。应用备份、精确保存及回读，不重载、不构建、不发布，不回退手拼格式。TypeScript尚未验证。",
          _OBJ({'project': _STR, 'name': _STR, 'folder': _STR, 'parent_file': _STR,
                'kind': {'type':'string','enum':['folder','view','content','usercontrol','codebehind_js','javascript','javascript_classic','css','function_js','function_js_classic']},
                'apply': {'type':'boolean'}}, ['kind','name']), False,
          lambda a: __import__('tc_template.hmi_items', fromlist=['create_item']).create_item(**a), 'HMI'),
    _tool("tc_hmi_item_delete",
          "默认预览；原生删除HMI页面、UserControl及参数、Function及描述、JS/CSS或空文件夹。保护启动/登录页，保守引用门禁、备份及文件/节点/工程/配置四项回读。配置清理可能重载一次，不发布或操作PLC。repair_orphan=true仅清理已缺失节点及文件的残留登记。",
          _OBJ({'file': _STR, 'project': _STR, 'apply': {'type':'boolean'}, 'repair_orphan': {'type':'boolean'}}, ['file']), False,
          lambda a: __import__('tc_template.hmi_delete', fromlist=['delete_item']).delete_item(**a), 'HMI'),
    _tool("tc_hmi_create_view",
          "新建 HMI 页面首选：在 controls 数组一次提交多个控件，不要先建空页再逐个添加。使用官方 AddView/AddContent + AddControl/ChangeAttributes 语义创建，不复制页面源码、不重载工程；默认 apply=false，写入后回读工程登记和控件数量。事件另用 tc_hmi_control_events。",
          _OBJ({
              "name": {**_STR, "description": "页面名；可省略 .view/.content 扩展名"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "kind": {"type": "string", "enum": ["view", "content"]},
              "controls": {"type": "array", "items": {
                  "type": "object", "properties": {
                  "id": _STR,
                  "type": {**_STR, "description": "完整控件类型，例如 TcHmi.Controls.Beckhoff.TcHmiButton"},
                  "parent_id": {**_STR, "description": "可选父控件 ID；省略时挂在页面根控件下"},
                  "attributes": {"type": "object", "additionalProperties": {"type": "string"}},
                  }, "required": ["id", "type"], "additionalProperties": False,
              }},
              "apply": {"type": "boolean", "description": "false=仅预览，true=创建并回读"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-create-view", project=str(a.get("project") or ""), name=a["name"],
                           kind=str(a.get("kind") or "view"), controls=list(a.get("controls") or []),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_project_api",
          "覆盖当前安装 TE2000 的官方 ITcHmiProject 成员。先用 operation=catalog 获取完整成员清单；读取操作直接执行，写入/构建/预览/发布操作默认只预览，必须 apply=true 才调用官方 API，并返回受限对象摘要和回读结果。",
          _OBJ({
              "operation": {**_STR, "description": "例如 catalog、get-config、change-config、build、publish、get-interface"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "arguments": {"type": "object", "description": "操作专用 JSON 参数；先查 catalog 或按 operation 语义传入"},
              "apply": {"type": "boolean", "description": "写入、构建、浏览器、发布等副作用操作是否执行；默认 false"},
          }, ["operation"]), False,
          lambda a: ps_com("hmi-project-api", project=str(a.get("project") or ""),
                           operation=a["operation"], arguments=dict(a.get("arguments") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_startup_view_set",
          "设置 HMI 启动页面；通过官方 ITcHmiProject.ChangeStartupView 精确校验已登记的 .view，保存并回读，不重载工程。",
          _OBJ({
              "view": {**_STR, "description": "项目内相对 .view 路径，例如 Dashboard.view"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
          }, ["view"]), False,
          lambda a: ps_com("hmi-startup-view-set", project=str(a.get("project") or ""), view=a["view"]), "HMI"),
    _tool("tc_hmi_control_events",
          "先 action=read 获取当前控件真实事件列表和 action_contract，再预览/增改/删除 Trigger。"
          "常规事件默认 placement=native，使用 .onPressed/.onStatePressed/.onStateReleased 等控件原生事件，"
          "显示在 XAE 下方 Framework/Operator/Control 事件组；只有明确特殊需求才用 placement=custom 写入上方 Custom。"
          "动作使用 objectType（不是 type/actionType/action）；WriteToSymbol 必须提供 symbolExpression 和"
          " value:{objectType:StaticValue,value:...}。不要猜事件名或动作键。DTE 文本编辑，不执行事件。",
          _OBJ({"file": _STR, "control_id": _STR, "project": _STR, "event": _STR,
                "action": {"type": "string", "enum": ["read", "upsert", "remove"]},
                "placement": {"type": "string", "enum": ["native", "custom"],
                              "description": "默认 native；custom 仅用于明确要求的上方 Custom 触发器"},
                "actions": {"type": "array", "items": ACTION_SCHEMA},
                "apply": {"type": "boolean"}}, ["file", "control_id"]), False,
          lambda a: control_events(a["file"], a["control_id"], a.get("project", ""),
                                   a.get("event", ""), a.get("actions", []),
                                   a.get("action", "read"), bool(a.get("apply", False)),
                                   a.get("placement", "native")), "HMI"),
    _tool("tc_hmi_control_schema",
          "常用属性优先复用版本匹配的速查记忆，陌生属性、复杂值或校验失败才查询。省略 control_type 列合法类型；指定类型列继承属性，指定 attribute 返回值 Schema。写入强制本地校验始终保留，不猜名称与枚举。",
          _OBJ({"project": _STR, "control_type": _STR, "attribute": _STR}), True,
          lambda a: hmi_control_schema(**a), "HMI"),
    _tool("tc_hmi_control_edit",
          "仅修改单个 HMI 控件时使用；同页多个控件用 tc_hmi_controls_batch。优先复用版本匹配的属性记忆，陌生值或报错再查 Schema；写入强制校验。"
          "参数示例：file='Desktop.view', action='update', control_id='BtnStart', attributes={data-tchmi-state-symbol:'%s%ADS.PLC1.GVL_Hmi.bStart%/s%'}, apply=true。"
          "attributes 必须是 JSON 对象而不是 JSON 字符串；apply 必须是布尔值。复杂属性的属性值才使用 JSON 字符串。DTE 保存回读，不重载项目，不代表浏览器/ADS 验证。",
          _OBJ({
              "file": {**_STR, "description": "项目相对 .view/.content/.usercontrol 路径"},
              "action": {"type": "string", "enum": ["add", "update", "remove"]},
              "control_id": {**_STR, "description": "目标控件 ID"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "control_type": {**_STR, "description": "add 时必填完整 TcHmi.Controls.* 类型"},
              "parent_id": {**_STR, "description": "add 时可选父控件 ID；默认根控件"},
              "attributes": {"type": "object", "additionalProperties": {
                  "anyOf": [{"type": "string"}, {"type": "null"}],
              }, "description": "data-tchmi-* 属性；值为 null 表示删除该属性"},
              "apply": {"type": "boolean", "description": "false=预览，true=DTE 保存并回读，不重载工程"},
          }, ["file", "action", "control_id"]), False,
          lambda a: ps_com("hmi-control-edit", project=str(a.get("project") or ""), file=a["file"],
                           action=a["action"], control_id=a["control_id"],
                           type=str(a.get("control_type") or ""), parent_id=str(a.get("parent_id") or ""),
                           attributes=dict(a.get("attributes") or {}), apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_controls_batch",
          "同页多个控件增删改首选：按 operations 顺序在内存编辑，一次 Schema 校验、备份、DTE 保存和整页回读。"
          "优先复用版本匹配的属性记忆，陌生值或报错再查 tc_hmi_control_schema；每批1..100项，每个control_id只出现一次，父控件先添加。"
          "不允许删除本批其他目标的父控件。复杂属性用JSON字符串，null删除属性。"
          "operations 必须是 JSON 数组而不是字符串；每项格式为 {action:'update', control_id:'BtnStop', attributes:{data-tchmi-state-symbol:'%s%ADS.PLC1.GVL_Hmi.bStop%/s%'}}，字段名不是 controls/id。"
          "任一预检失败不写入；未保存或并发变更拒绝覆盖。默认预览，不重载工程，不代表浏览器/ADS验证。",
          _OBJ({"file": _STR, "project": _STR,
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                    "type": "object", "properties": {
                        "action": {"type": "string", "enum": ["add", "update", "remove"]},
                        "control_id": _STR, "control_type": _STR, "parent_id": _STR,
                        "attributes": {"type": "object", "additionalProperties": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]}},
                    }, "required": ["action", "control_id"], "additionalProperties": False}},
                "apply": {"type": "boolean"}}, ["file", "operations"]), False,
          lambda a: ps_com("hmi-controls-batch", project=str(a.get("project") or ""), file=a["file"],
                           operations=a["operations"], apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_delete_view",
          "预览或删除非启动 HMI View/Content；apply=true 时先在项目内创建可恢复备份，再删除工程登记和文件并重载回读。",
          _OBJ({
              "file": {**_STR, "description": "项目相对 .view/.content 路径"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "apply": {"type": "boolean", "description": "false=预览，true=备份、删除并回读"},
          }, ["file"]), False,
          lambda a: ps_com("hmi-delete-view", project=str(a.get("project") or ""), file=a["file"],
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_ads_runtime_set",
          "预览或增改/删除 HMI ADS Runtime 配置；校验 NetId/端口，保留已有符号映射，apply=true 后重载并回读。不会连接 PLC。",
          _OBJ({
              "name": {**_STR, "description": "ADS Runtime 名称"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "scope": {"type": "string", "enum": ["default", "remote", "both"]},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "properties": {
                  "netid": _STR, "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                  "enabled": {"type": "boolean"}, "read_only": {"type": "boolean"},
                  "use_whitelisting": {"type": "boolean"},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-ads-runtime-set", project=str(a.get("project") or ""), name=a["name"],
                           scope=str(a.get("scope") or "both"), action=str(a.get("action") or "upsert"),
                           settings=dict(a.get("settings") or {}), apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_ads_symbols",
          "只读查询已保存的静态/动态映射，默认 both；动态项返回可用 page_expression。symbol_count 是静态数量，不代表动态映射为空。不连接 PLC，勿用空 dynamic_symbols_set 查询。",
          _OBJ({
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "runtime": {**_STR, "description": "可选 Runtime 名称过滤"},
              "scope": {"type": "string", "enum": ["", "default", "remote"]},
              "mapping_kind": {"type": "string", "enum": ["static", "dynamic", "both"]},
          }), True, lambda a: __import__('tc_template.hmi_mapping_catalog', fromlist=['read_mappings']).read_mappings(**a), "HMI"),
    _tool("tc_hmi_ads_symbol_set",
          "预览静态 ADS 地址映射或删除既有映射。Agent 禁止直接应用未经符号解析验证的数字地址；新增绑定使用 tc_hmi_bind_plc 的 TMC 动态映射，不猜地址。",
          _OBJ({
              "runtime": {**_STR, "description": "已配置的 ADS Runtime 名称"},
              "name": {**_STR, "description": "映射符号名，通常为 PLC 符号路径"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "scope": {"type": "string", "enum": ["default", "remote", "both"]},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "index_group": {"type": "integer", "minimum": 0, "maximum": 4294967295},
              "index_offset": {"type": "integer", "minimum": 0, "maximum": 4294967295},
              "type_name": {**_STR, "description": "upsert 必填，例如 BOOL、DINT"},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["runtime", "name"]), False,
          lambda a: ps_com("hmi-ads-symbol-set", project=str(a.get("project") or ""),
                           runtime=a["runtime"], name=a["name"], scope=str(a.get("scope") or "both"),
                           action=str(a.get("action") or "upsert"), index_group=int(a.get("index_group") or 0),
                           index_offset=int(a.get("index_offset") or 0), type_name=str(a.get("type_name") or ""),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_bindings",
          "只读清点页面 SymbolExpression，并静态检查 Server Runtime/映射、内部符号和控件 ID 引用；不运行浏览器或 HMI Server。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-bindings", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_dynamic_symbols_set",
          "低层工具：预览或批量登记 TcHmiSrv 动态 ADS 符号及类型定义。每项必须提供 DOMAIN=ADS、DYNAMIC=true、USEMAPPING=true、MAPPING 和 SCHEMA；可接受同名 camelCase 键并规范化。不会猜 IndexGroup/IndexOffset；通常优先 tc_hmi_bind_plc 从实际 TMC 生成。重载失败必须报告失败并回滚。",
          _OBJ({
              "project": _STR,
              "symbols": {"type": "object", "additionalProperties": {"type": "object"}},
              "definitions": {"type": "object", "additionalProperties": {"type": "object"}},
              "apply": {"type": "boolean"},
          }, ["symbols"]), False,
          lambda a: ps_com("hmi-dynamic-symbols-set", project=str(a.get("project") or ""),
                           symbols=dict(a.get("symbols") or {}),
                           definitions=dict(a.get("definitions") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_variable_search",
          "HMI 找 PLC 变量首选：实时 ADS 元数据搜索，按 parent 展开结构/数组，query/type_filter 筛选，next_offset 分页。auto 在线失败才退到 TMC 并标明离线；不读取值。已映射变量返回 binding_candidate，原样交给 tc_hmi_bind_variable。无映射根用 tc_hmi_bind_plc，禁止猜路径。",
          _OBJ({'project': _STR, 'plc': _STR, 'query': _STR, 'parent': _STR,
                'type_filter': _STR, 'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                'source': {'type': 'string', 'enum': ['auto', 'ads', 'tmc']}}), True,
          lambda a: __import__('tc_template.hmi_variable_discovery', fromlist=['search_variables']).search_variables(**a), 'HMI'),
    _tool("tc_hmi_bind_variable",
          "将 tc_hmi_variable_search 返回的 binding_candidate 原样绑定到控件属性。写前查 tc_hmi_control_schema；自动重核端点/在线类型/映射，失败要求重新搜索。仅使用已有映射，默认预览，应用走 DTE 不重载，不写 PLC 值。",
          _OBJ({'candidate': {'type': 'object'}, 'file': _STR, 'control_id': _STR,
                'attribute': _STR, 'apply': {'type': 'boolean'}},
               ['candidate', 'file', 'control_id', 'attribute']), False,
          lambda a: __import__('tc_template.hmi_variable_discovery', fromlist=['bind_candidate']).bind_candidate(**a), 'HMI'),
    _tool("tc_hmi_bind_plc",
          "从当前 XAE 读取真实目标 NetId 和指定 PLC 的实际 ADS 端口，解析 TMC 并原子配置 HMI ADS Runtime、动态符号和 Schema。返回的 page_expression 才能写入页面（%s%ADS.<Runtime>...%/s%）；MAPPING 的 <Runtime>::... 仅供 Server 内部使用，禁止写入页面。多 PLC 时必须指定 plc；不激活、不重启、不登录 PLC。",
          _OBJ({
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "plc": {**_STR, "description": "PLC 项目/runtime 名；多 PLC 时必填"},
              "runtime_name": {**_STR, "description": "HMI ADS Runtime 名；默认复用唯一 Runtime 或 PLC1"},
              "symbol_roots": {"type": "array", "items": _STR, "description": "要公开的 PLC 根符号；默认 GVL_Hmi"},
              "read_only_symbols": {"type": "array", "items": _STR, "description": "按根路径设置只读的符号"},
              "scope": {"type": "string", "enum": ["default", "both"]},
              "apply": {"type": "boolean", "description": "false=预览，true=事务写入、单次重载并回读"},
          }), False,
          lambda a: bind_hmi_plc(
              project=str(a.get("project") or ""), plc=str(a.get("plc") or ""),
              runtime_name=str(a.get("runtime_name") or ""),
              symbol_roots=list(a.get("symbol_roots") or []),
              read_only_symbols=list(a.get("read_only_symbols") or []),
              scope=str(a.get("scope") or "default"), apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_internal_symbols",
          "只读列出 HMI 项目的内部符号，包括 tchmi 类型引用、默认值、持久化和只读标志。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-internal-symbols", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_internal_symbol_set",
          "预览或增改/删除一个 HMI 内部符号；type 必须是 tchmi: schema 引用，apply=true 后重载并回读。",
          _OBJ({
              "name": {**_STR, "description": "内部符号名称"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "properties": {
                  "type": {**_STR, "description": "例如 tchmi:general#/definitions/Boolean"},
                  "value": {}, "persist": {"type": "boolean"}, "readonly": {"type": "boolean"},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-internal-symbol-set", project=str(a.get("project") or ""), name=a["name"],
                           action=str(a.get("action") or "upsert"), settings=dict(a.get("settings") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_localizations",
          "只读列出 HMI 项目的注册语言、本地化文件和文本键，并指出各语言缺失的键。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-localizations", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_localization_set",
          "预览或跨已注册语言增改/删除一个 HMI 本地化键；apply=true 后重载项目并回读。",
          _OBJ({
              "key": {**_STR, "description": "本地化键，例如 StartButton"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "values": {"type": "object", "description": "locale 到文本或 null 的映射，例如 {en: Start, de: Starten}",
                         "additionalProperties": {"type": ["string", "null"]}},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["key"]), False,
          lambda a: ps_com("hmi-localization-set", project=str(a.get("project") or ""), key=a["key"],
                           action=str(a.get("action") or "upsert"), values=dict(a.get("values") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_themes",
          "只读列出 HMI 主题、主题资源文件、当前活动主题和项目级 ThemedResource 值。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-themes", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_themed_resource_set",
          "预览或增改/删除一个项目级 HMI ThemedResource；values 按已注册主题名设置，apply=true 后回读。",
          _OBJ({
              "name": {**_STR, "description": "资源名；页面中以 %tr%名称%/tr% 引用"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "properties": {
                  "type": {**_STR, "description": "tchmi: schema 类型引用"},
                  "description": _STR,
                  "values": {"type": "object", "additionalProperties": {}},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-themed-resource-set", project=str(a.get("project") or ""), name=a["name"],
                           action=str(a.get("action") or "upsert"), settings=dict(a.get("settings") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_active_theme_set",
          "预览或设置 HMI 启动时的活动主题；只能选择工程中已注册的主题，apply=true 后重载回读。",
          _OBJ({
              "theme": {**_STR, "description": "已注册主题名，例如 Base 或 Base-Dark"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["theme"]), False,
          lambda a: ps_com("hmi-active-theme-set", project=str(a.get("project") or ""), theme=a["theme"],
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_user_controls",
          "只读列出 HMI UserControl、配套参数文件、参数定义和内部控件。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-user-controls", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_user_control_create",
          "按本机 1.12 模板预览或创建 UserControl，同时登记主文件、参数文件和 tchmiconfig；apply=true 后回读。",
          _OBJ({
              "name": {**_STR, "description": "UserControl 名称，不含路径"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "parameters": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
              "controls": {"type": "array", "items": {"type": "object", "properties": {
                  "id": _STR, "type": _STR,
                  "attributes": {"type": "object", "additionalProperties": {"type": "string"}},
              }, "required": ["id", "type"], "additionalProperties": False}},
              "apply": {"type": "boolean", "description": "false=预览，true=创建、重载并回读"},
          }, ["name"]), False,
          lambda a: ps_com("hmi-user-control-create", project=str(a.get("project") or ""), name=a["name"],
                           parameters=list(a.get("parameters") or []), controls=list(a.get("controls") or []),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_user_control_parameter_set",
          "按 UserControlConfig.Schema.json 预览或增改/删除一个 UserControl 参数；apply=true 后重载回读。",
          _OBJ({
              "user_control": {**_STR, "description": "UserControl 名称或 .usercontrol 文件名"},
              "name": {**_STR, "description": "内部参数名，必须以 data-tchmi- 开头"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "additionalProperties": True},
              "apply": {"type": "boolean", "description": "false=预览，true=写入、重载并回读"},
          }, ["user_control", "name"]), False,
          lambda a: ps_com("hmi-user-control-parameter-set", project=str(a.get("project") or ""),
                           user_control=a["user_control"], name=a["name"],
                           action=str(a.get("action") or "upsert"), settings=dict(a.get("settings") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_user_control_delete",
          "预览或备份并删除一个 HMI UserControl，同时移除主文件、参数文件、hmiproj 和 tchmiconfig 登记。",
          _OBJ({
              "user_control": {**_STR, "description": "UserControl 名称或 .usercontrol 文件名"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "apply": {"type": "boolean", "description": "false=预览，true=备份、删除并回读"},
          }, ["user_control"]), False,
          lambda a: ps_com("hmi-user-control-delete", project=str(a.get("project") or ""),
                           user_control=a["user_control"], apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_framework_templates",
          "只读列出本机 TE2000 Framework Project 模板，并返回当前 HMI 工程的 Framework 目标版本。",
          _OBJ({"project": {**_STR, "description": "用于确定 Framework 版本的 HMI 项目"}}),
          True, lambda a: ps_com("hmi-framework-templates", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_framework_validate",
          "只读校验 .hmiextproj、Manifest、Control Description、模板、脚本、主题资源和类型 schema。",
          _OBJ({"source": {**_STR, "description": ".hmiextproj 文件或只含一个该项目的目录"}}, ["source"]),
          True, lambda a: ps_com("hmi-framework-validate", source=a["source"]), "HMI"),
    _tool("tc_hmi_framework_control_info",
          "只读返回一个 Framework Control 的 Description、源码路径、属性、函数和事件契约。",
          _OBJ({
              "source": {**_STR, "description": ".hmiextproj 文件或项目目录"},
              "control": {**_STR, "description": "多控件项目中指定 control name 或 basePath"},
          }, ["source"]), True,
          lambda a: ps_com("hmi-framework-control-info", source=a["source"],
                           control=str(a.get("control") or "")), "HMI"),
    _tool("tc_hmi_framework_attribute_set",
          "预览或同步增删 Framework Control 属性：更新 Description.json，并生成受控 getter/setter/process 代码区。",
          _OBJ({
              "source": {**_STR, "description": ".hmiextproj 文件或项目目录"},
              "name": {**_STR, "description": "PascalCase propertyName，如 StatusText"},
              "control": {**_STR, "description": "多控件项目中指定 control name 或 basePath"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "additionalProperties": True,
                           "description": "支持 html_name/type/value_kind/category/default_value 等"},
              "apply": {"type": "boolean", "description": "false=预览，true=备份、写入并结构回读"},
          }, ["source", "name"]), False,
          lambda a: ps_com("hmi-framework-attribute-set", source=a["source"],
                           control=str(a.get("control") or ""), name=a["name"],
                           action=str(a.get("action") or "upsert"), settings=dict(a.get("settings") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_framework_event_set",
          "预览或同步增删 Framework Control 自定义事件：更新 Description.json，并生成 EventProvider.raise helper。",
          _OBJ({
              "source": {**_STR, "description": ".hmiextproj 文件或项目目录"},
              "name": {**_STR, "description": "事件名，如 ValueChanged、onValueChanged 或 .onValueChanged"},
              "control": {**_STR, "description": "多控件项目中指定 control name 或 basePath"},
              "action": {"type": "string", "enum": ["upsert", "remove"]},
              "settings": {"type": "object", "additionalProperties": True,
                           "description": "支持 category/display_priority/arguments 等"},
              "apply": {"type": "boolean", "description": "false=预览，true=备份、写入并结构回读"},
          }, ["source", "name"]), False,
          lambda a: ps_com("hmi-framework-event-set", source=a["source"],
                           control=str(a.get("control") or ""), name=a["name"],
                           action=str(a.get("action") or "upsert"), settings=dict(a.get("settings") or {}),
                           apply=bool(a.get("apply", False))), "HMI"),
    _tool("tc_hmi_framework_create",
          "从本机 TE2000 官方模板预览或创建 native1.12 Framework Control 工程；不加入解决方案、不打包、不发布。",
          _OBJ({
              "name": {**_STR, "description": "Framework 项目名，使用字母、数字和下划线"},
              "output_directory": {**_STR, "description": "已存在的输出父目录"},
              "project": {**_STR, "description": "用于确定 Framework 版本的 HMI 项目"},
              "language": {"type": "string", "enum": ["typescript", "javascript"]},
              "description": {**_STR, "description": "NuGet 包描述"},
              "apply": {"type": "boolean", "description": "false=预览，true=创建并结构校验"},
          }, ["name", "output_directory"]), False,
          lambda a: ps_com("hmi-framework-create", project=str(a.get("project") or ""),
                           name=a["name"], output_directory=a["output_directory"],
                           language=str(a.get("language") or "typescript"),
                           description=str(a.get("description") or ""),
                           apply=bool(a.get("apply", False)), timeout=120.0), "HMI"),
    _tool("tc_hmi_framework_pack",
          "预览或用 TE2000 随附 NuGet 工具生成并校验本地 Framework .nupkg；不会安装进 HMI 工程或发布。",
          _OBJ({
              "source": {**_STR, "description": ".hmiextproj 文件或项目目录"},
              "output_directory": {**_STR, "description": "可选输出目录，默认项目 bin/Packages"},
              "version": {**_STR, "description": "可选包版本，默认读取 nuspec"},
              "apply": {"type": "boolean", "description": "false=预览，true=生成并检查包内容"},
          }, ["source"]), False,
          lambda a: ps_com("hmi-framework-pack", source=a["source"],
                           output_directory=str(a.get("output_directory") or ""),
                           version=str(a.get("version") or ""), apply=bool(a.get("apply", False)),
                           timeout=120.0), "HMI"),
    _tool("tc_hmi_framework_packages",
          "只读清点当前工程真实 HMI NuGet 包版本和 package_path；路径来自实际文件，package_exists=false 时不得猜 .nuget 目录或拼文件名。检查包请原样复制 package_path；framework_inspection_applicable=false 的 Server/构建依赖不适用 Framework 包检查。也核对工程注册与虚拟目录。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
                "package_id": {**_STR, "description": "可选精确包 ID，用于缩小清单；版本从工程读取"}}),
          True, lambda a: ps_com("hmi-framework-packages", project=str(a.get("project") or ""),
                                package_id=str(a.get("package_id") or "")), "HMI"),
    _tool("tc_hmi_framework_package_inspect",
          "只读检查 HMI 控件包、函数包、框架包或资源包的 .nupkg，返回模块分类、控件/函数数量及依赖。先用 tc_hmi_framework_packages 获取真实 package_path，不猜包名/版本/缓存目录。不安装或运行包。",
          _OBJ({"package": {**_STR, "description": "清单返回的真实 package_path，或用户明确给出的 .nupkg 文件路径"}}, ["package"]),
          True, lambda a: ps_com("hmi-framework-package-inspect", package=a["package"]), "HMI"),
    _tool("tc_hmi_framework_install",
          "预览或事务安装 Framework Control 包；同步四处配置、XAE 重载回读，失败自动回滚。apply 必须硬确认。",
          _OBJ({
              "package": {**_STR, "description": "已通过检查的本地 .nupkg 文件"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "apply": {"type": "boolean", "description": "false=预览，true=执行安装"},
              "acknowledge_package_change": {"type": "boolean", "description": "apply 时必须为 true，确认修改工程包依赖"},
          }, ["package"]), False,
          lambda a: ps_com("hmi-framework-install", package=a["package"],
                           project=str(a.get("project") or ""), apply=bool(a.get("apply", False)),
                           acknowledge_package_change=bool(a.get("acknowledge_package_change", False)),
                           timeout=180.0), "HMI"),
    _tool("tc_hmi_framework_uninstall",
          "预览或事务卸载非核心 Framework Control 包；先扫描页面引用，默认阻止有引用的卸载，apply 必须硬确认。",
          _OBJ({
              "package_id": {**_STR, "description": "packages.config 中的精确包 ID"},
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "force": {"type": "boolean", "description": "仅在审查引用后允许带引用卸载"},
              "apply": {"type": "boolean", "description": "false=预览，true=执行卸载"},
              "acknowledge_package_change": {"type": "boolean", "description": "apply 时必须为 true，确认修改工程包依赖"},
          }, ["package_id"]), False,
          lambda a: ps_com("hmi-framework-uninstall", package_id=a["package_id"],
                           project=str(a.get("project") or ""), force=bool(a.get("force", False)),
                           apply=bool(a.get("apply", False)),
                           acknowledge_package_change=bool(a.get("acknowledge_package_change", False)),
                           timeout=180.0), "HMI"),
    _tool("tc_hmi_runtime_info",
          "只读发现当前 HMI Engineering Server 进程、Endpoint、虚拟目录和真实应用入口；不会启动 Server 或发布。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          True, lambda a: ps_com("hmi-runtime-info", project=str(a.get("project") or "")), "HMI"),
    _tool("tc_hmi_server_control",
          "预览或启动、停止、重启当前工程对应的 HMI Engineering Server。只处理命令行 storageDir 精确匹配的进程，后台启动且不发布、不构建、不操作 PLC。",
          _OBJ({
              "action": {"type": "string", "enum": ["start", "stop", "restart"]},
              "project": {**_STR, "description": "HMI 项目名或 .hmiproj 路径"},
              "apply": {"type": "boolean", "description": "false=预览，true=执行并健康回读"},
          }, ["action"]), False,
          lambda a: ps_com("hmi-server-control", project=str(a.get("project") or ""),
                           action=a["action"], apply=bool(a.get("apply", False)), timeout=60.0), "HMI"),
    _tool("tc_hmi_ads_live_check",
          "只读核对 HMI default Runtime 与当前 XAE PLC 的真实 NetId/ADS 端口，并通过 TcAdsDll 在线读取已配置动态符号；不会写变量、启动 Server 或启动 PLC。",
          _OBJ({
              "project": {**_STR, "description": "HMI 项目名或 .hmiproj 路径"},
              "runtime": {**_STR, "description": "HMI ADS Runtime 名；多个启用项时必填"},
              "plc": {**_STR, "description": "PLC runtime 名；多 PLC 且端口无法唯一匹配时必填"},
              "symbols": {"type": "array", "items": _STR, "maxItems": 32},
              "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
              "max_symbols": {"type": "integer", "minimum": 1, "maximum": 32},
          }), True,
          lambda a: check_hmi_ads_online(
              project=str(a.get("project") or ""), runtime=str(a.get("runtime") or ""),
              plc=str(a.get("plc") or ""), symbols=list(a.get("symbols") or []),
              max_depth=int(a.get("max_depth") or 3), max_symbols=int(a.get("max_symbols") or 32)), "HMI"),
    _tool("tc_hmi_binding_diagnose",
          "HMI 变量绑定故障的首选只读入口：一次区分页面表达式、TMC 导出、Server 动态映射/Schema、HMI Runtime 端点和 PLC ADS 在线状态，并返回按故障阶段排序的修复工具；不会切换目标、登录/启动 PLC、重载 HMI 或写配置。",
          _OBJ({
              "project": {**_STR, "description": "HMI 项目名或 .hmiproj 路径"},
              "runtime": {**_STR, "description": "HMI ADS Runtime 名；多个启用项时必填"},
              "plc": {**_STR, "description": "PLC runtime 名；多 PLC 时必填"},
              "max_symbols": {"type": "integer", "minimum": 1, "maximum": 32,
                              "description": "一次在线验证的页面已映射变量上限"},
          }), True,
          lambda a: com_hmi_binding_diagnose(
              project=str(a.get("project") or ""), runtime=str(a.get("runtime") or ""),
              plc=str(a.get("plc") or ""), max_symbols=int(a.get("max_symbols") or 32)), "HMI"),
    _tool("tc_hmi_browser_validate",
          "在临时隐藏浏览器中加载已运行的 HMI；可用 entry_page 明确加载 Main.view 等已保存页面。采集 JavaScript/Console/HTTP/WebSocket 错误、控件/绑定数量和视口溢出；不点击、不写 PLC、不启动 Server。",
          _OBJ({
              "project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
              "widths": {"type": "array", "items": {"type": "integer", "minimum": 320, "maximum": 3840},
                         "maxItems": 4, "description": "要检查的视口宽度，默认 [1280]"},
              "height": {"type": "integer", "minimum": 240, "maximum": 2160},
               "settle_ms": {"type": "integer", "minimum": 500, "maximum": 30000,
                             "description": "页面加载后的等待时间，默认 5000 ms"},
               "entry_page": {**_STR, "description": "要明确加载验证的项目相对 .view；省略则验证启动页"},
           }), True,
          lambda a: com_hmi_browser_validate(project=str(a.get("project") or ""),
                           widths=[int(v) for v in (a.get("widths") or [1280])],
                           height=int(a.get("height") or 720), settle_ms=int(a.get("settle_ms") or 5000),
                           entry_page=str(a.get("entry_page") or "")), "HMI"),
    _tool("tc_hmi_diagnostics",
          "只读读取当前 XAE 错误列表（不可读时明确标记）、工程 HMI Server 最近一小时日志及已有页面 HTTP 状态。历史故障不等于当前故障，HTTP 成功不代表 PLC 通信正常；不编译、不清空列表、不启动 Server/PLC。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"},
                "check_page": {"type": "boolean", "description": "是否只读探测已运行的 HMI HTTP 入口，默认 true"}}),
          True, lambda a: read_hmi_diagnostics(str(a.get("project") or ""), bool(a.get("check_page", True))), "HMI"),
    _tool("tc_hmi_build",
          "构建 HMI 并分别返回 build_succeeded、错误列表可用性、Server 历史诊断。incomplete 不是零错误；诊断缺失用 tc_hmi_diagnostics，不要重复编译。不验证页面交互或 PLC 通信。",
          _OBJ({"project": {**_STR, "description": "可选 HMI 项目名或 .hmiproj 路径"}}),
          False, lambda a: com_hmi_build(project=str(a.get("project") or "")), "HMI"),
    _tool("tc_system_structure",
          "只读读取 XAE SYSTEM 的实时树（TIRC/TIRS/TIRT），返回节点路径、ItemType 和子节点；不写配置。",
          _OBJ({
              "max_depth": {"type": "integer", "minimum": 0, "maximum": 12},
              "roots": {"type": "array", "items": {"type": "string", "enum": ["TIRC", "TIRS", "TIRT"]},
                        "description": "可选读取根节点，默认 TIRC、TIRS、TIRT"},
          }), True,
          lambda a: ps_com("system-structure", max_depth=int(a.get("max_depth") or 6),
                           roots=list(a.get("roots") or [])), "系统"),
    _tool("tc_system_settings",
          "只读读取 SYSTEM > Real-Time > Settings 的 ProduceXml、逐核 P-Core/E-Core 标签、选中状态、Affinity 和实时任务属性。",
          _OBJ(), True, lambda a: ps_com("system-settings"), "系统"),
    _tool("tc_realtime_info",
          "只读汇总 TwinCAT Real-Time：明确区分 4024/4026 的 Router Memory 语义，返回每任务堆栈、"
          "逐核 Base Time/Core Limit/Core Memory、任务周期和优先级；不写配置。",
          _OBJ(), True, lambda a: ps_com("realtime-info"), "系统"),
    _tool("tc_realtime_validate",
          "只读审查完整 TwinCAT Real-Time 配置：检查 4024/4026 内存语义、实时核选择、"
          "Base Time、Core Limit、任务优先级重复以及任务周期兼容性；不写配置。",
          _OBJ(), True,
          lambda a: (lambda snapshot: {
              **validate_realtime_snapshot(snapshot), "snapshot": snapshot,
          })(ps_com("realtime-info")), "系统"),
    _tool("tc_system_settings_set",
          "预览或写入白名单 SYSTEM 实时设置。默认只预览；必须显式 apply=true 才会 ConsumeXml，写入后自动回读验证。"
          "不会自动切换 Config、激活或重启。",
          _OBJ({
              "settings": {"type": "object", "properties": {
                  "max_cpus": {"type": "integer", "minimum": 1, "maximum": 4095},
                  "cpu_ids": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 4095}, "minItems": 1, "uniqueItems": True},
                  "affinity": {"type": "integer", "minimum": 0,
                               "description": "整体实时核心位掩码；例如核心0、12、14为20481（#x5001）"},
                  "p_core_affinity": {"type": "integer", "minimum": 0},
                  "e_core_affinity": {"type": "integer", "minimum": 0},
                  "router_memory_mb": {"type": "integer", "minimum": 1, "maximum": 65535,
                                       "description": "4024 为合并 RT/ADS 内存且最大 1024MB；4026 为 Global RT Memory"},
                  "max_stack_size_kb": {"type": "integer", "minimum": 1, "maximum": 1048576,
                                        "description": "每个实时 Task 的最大堆栈，单位 KB"},
                  "core_settings": {"type": "array", "minItems": 1, "items": {
                      "type": "object", "properties": {
                          "cpu_id": {"type": "integer", "minimum": 0, "maximum": 4095},
                          "base_time_100ns": {"type": "integer", "minimum": 1},
                          "load_limit_percent": {"type": "integer", "minimum": 1, "maximum": 100},
                          "latency_warning_100ns": {"type": "integer", "minimum": 0},
                          "core_memory_kb": {"type": "integer", "minimum": 0,
                                             "description": "仅 4026；指定实时核专用内存，单位 KB"},
                      }, "required": ["cpu_id"], "additionalProperties": False,
                  }},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入并回读"},
          }, ["settings"]), False,
          lambda a: ps_com("system-settings-set", settings=dict(a["settings"]),
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_realtime_settings_set",
          "预览或写入 Router Memory、每任务最大堆栈和逐核 Base Time/Core Limit/Latency/Core Memory。"
          "自动识别 4024/4026：4024 禁止 Core Memory 且 Router Memory 上限 1024MB；4026 将 Router Memory"
          "解释为 Global RT Memory。默认预览，apply=true 才写入；不自动激活或重启。",
          _OBJ({
              "settings": {"type": "object", "properties": {
                  "router_memory_mb": {"type": "integer", "minimum": 1, "maximum": 65535},
                  "max_stack_size_kb": {"type": "integer", "minimum": 1, "maximum": 1048576},
                  "core_settings": {"type": "array", "minItems": 1, "items": {
                      "type": "object", "properties": {
                          "cpu_id": {"type": "integer", "minimum": 0, "maximum": 4095},
                          "base_time_100ns": {"type": "integer", "minimum": 1},
                          "load_limit_percent": {"type": "integer", "minimum": 1, "maximum": 100},
                          "latency_warning_100ns": {"type": "integer", "minimum": 0},
                          "core_memory_kb": {"type": "integer", "minimum": 0},
                      }, "required": ["cpu_id"], "additionalProperties": False,
                  }},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入并回读"},
          }, ["settings"]), False,
          lambda a: ps_com("system-settings-set", settings=dict(a["settings"]),
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_core_info",
          "只读读取 TwinCAT 当前可用/已分配 CPU 核、逐核 P-Core/E-Core 标签、选中状态、Affinity 以及实时任务摘要。",
          _OBJ(), True, lambda a: ps_com("core-info"), "系统"),
    _tool("tc_core_assign",
          "预览或分配 TwinCAT 使用的 CPU 核。默认只预览；apply=true 才写入 TIRS 并回读验证。"
          "cpu_ids 同时用于更新 CPU 节点和整体 Affinity；未显式提供 affinity 时按 cpu_ids 自动生成。"
          "不自动激活或重启，避免未经确认改变实时调度。",
          _OBJ({
              "cpu_ids": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 4095}, "minItems": 1, "uniqueItems": True},
              "max_cpus": {"type": "integer", "minimum": 1, "maximum": 4095},
              "affinity": {"type": "integer", "minimum": 0,
                            "description": "整体实时核心位掩码；省略时按 cpu_ids 自动生成"},
              "p_core_affinity": {"type": "integer", "minimum": 0},
              "e_core_affinity": {"type": "integer", "minimum": 0},
              "apply": {"type": "boolean", "description": "false=预览，true=写入并回读"},
          }, ["cpu_ids"]), False,
          lambda a: ps_com("core-assign", cpu_ids=list(a["cpu_ids"]),
                           max_cpus=a.get("max_cpus"), p_core_affinity=a.get("p_core_affinity"),
                           affinity=a.get("affinity"),
                           e_core_affinity=a.get("e_core_affinity"),
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_task_info",
          "只读读取 TIRT 实时任务树和每个任务可见的 XML 参数。",
          _OBJ({"max_depth": {"type": "integer", "minimum": 0, "maximum": 12}}), True,
          lambda a: ps_com("task-info", max_depth=int(a.get("max_depth") or 6)), "系统"),
    _tool("tc_task_runtime_info",
          "通过实际 PLC Runtime ADS 端口读取 PlcTaskSystemInfo 运行指标，包括周期、优先级、"
          "执行次数、超周期和 RT Violation。多 PLC 时必须用 runtime 或 ads_port 明确选择；"
          "若系统变量未导出会明确返回 unavailable，不以静态配置冒充运行数据。",
          _OBJ({
              "runtime": {**_STR, "description": "PLC runtime/项目/实例名称；多 PLC 时必填其一"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "task_index": {"type": "integer", "minimum": 0, "maximum": 255},
          }), True, _task_runtime_info, "系统"),
    _tool("tc_task_core_assign",
          "预览或给指定 TIRT 任务分配 CPU 核。只修改 XAE XML 已明确暴露的 CpuId/CoreId/AssignedCore 字段，"
          "默认预览，apply=true 才写入并回读。若当前 XAE 未暴露该字段会拒绝写入。",
          _OBJ({
              "task_path": {**_STR, "description": "TIRT 下的精确任务路径"},
              "cpu_id": {"type": "integer", "minimum": 0, "maximum": 4095},
              "apply": {"type": "boolean", "description": "false=预览，true=写入并回读"},
          }, ["task_path", "cpu_id"]), False,
          lambda a: ps_com("task-core-assign", task_path=a["task_path"], cpu_id=a["cpu_id"],
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_task_settings_set",
          "预览或修改一个实时 Task 的优先级、扫描周期、AutoStart、相位和看门狗字段。"
          "自动检查优先级重复，以及 Cycle Time 是否为实时核 Base Time 的整数倍；默认预览。",
          _OBJ({
              "task_path": {**_STR, "description": "TIRT 下精确任务路径"},
              "settings": {"type": "object", "properties": {
                  "priority": {"type": "integer", "minimum": 0, "maximum": 31},
                  "cycle_time_100ns": {"type": "integer", "minimum": 1},
                  "cycle_time_us": {"type": "number", "exclusiveMinimum": 0},
                  "auto_start": {"type": "boolean"},
                  "tick_modulo": {"type": "integer", "minimum": 0},
                  "input_update_pre_ticks": {"type": "integer", "minimum": 0},
                  "exceed_warning": {"type": "integer", "minimum": 0},
                  "watchdog_stack_capacity": {"type": "integer", "minimum": 0},
              }, "additionalProperties": False},
              "apply": {"type": "boolean", "description": "false=预览，true=写入并回读"},
          }, ["task_path", "settings"]), False,
          lambda a: ps_com("task-settings-set", task_path=a["task_path"],
                           settings=dict(a["settings"]), apply=bool(a.get("apply", False))),
          "系统", "system"),
    _tool("tc_system_add",
          "预览或在 TIRC/TIRT 指定父节点下通过 COM CreateChild 添加 SYSTEM 子节点。"
          "默认预览；必须显式 apply=true 才写入，item_type 必须由目标 XAE/文档确认。",
          _OBJ({
              "parent_path": {**_STR, "description": "精确 TIRC 或 TIRT 父路径"},
              "name": _STR,
              "item_type": {"type": "integer", "minimum": 0, "maximum": 65535,
                             "description": "TwinCAT ITcSmTreeItem 子类型，不猜测"},
              "info": {**_STR, "description": "CreateChild 的 vInfo，可留空"},
              "apply": {"type": "boolean", "description": "false=预览，true=创建并回读"},
          }, ["parent_path", "name", "item_type"]), False,
          lambda a: ps_com("system-add", parent_path=a["parent_path"], name=a["name"],
                           item_type=a["item_type"], info=a.get("info", ""),
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_system_remove",
          "预览或删除 TIRC/TIRT 下一个精确 SYSTEM 节点；禁止删除 SYSTEM 根，含子节点时必须显式允许。",
          _OBJ({
              "path": {**_STR, "description": "精确 SYSTEM 节点路径"},
              "allow_with_children": {"type": "boolean"},
              "apply": {"type": "boolean", "description": "false=预览，true=删除并回读确认"},
          }, ["path"]), False,
           lambda a: ps_com("system-remove", path=a["path"],
                           allow_with_children=bool(a.get("allow_with_children", False)),
                           apply=bool(a.get("apply", False))), "系统", "system"),
    _tool("tc_realtime_refresh",
          "刷新当前已打开的 XAE SYSTEM > Real-Time 页面。只调用 XAE 刷新命令，不写 TIRS、不激活、不重启、不自动切页或抢焦点；页面未处于当前编辑上下文时明确失败。",
          _OBJ(), True, lambda a: ps_com("realtime-refresh"), "系统"),
    _tool("tc_safety_structure",
          "只读读取 XAE SAFETY(TISC) 项目树；不修改、编译、下载或激活安全配置。",
          _OBJ({"max_depth": {"type": "integer", "minimum": 0, "maximum": 12}}),
          True, lambda a: ps_com("safety-structure", max_depth=int(a.get("max_depth") or 8)),
          "Safety"),
    _tool("tc_safety_project_info",
          "只读读取 TISC 下 Safety 项目及可由 ProduceXml 确认的目标、语言、作者、CRC/Checksum 元数据。",
          _OBJ({"project": {**_STR, "description": "可选精确项目名或 TISC 路径；省略则列出全部"}}),
          True, lambda a: ps_com("safety-project-info", project=str(a.get("project") or "")),
          "Safety"),
    _tool("tc_safety_files",
          "只读清点并分类 Safety 项目的 .splcproj、TargetSystemConfig、SAL/GRP、SDS Alias Device、"
          "Safety C 与支持文件；project 可传 TISC 项目名/路径或本地 .splcproj/目录。",
          _OBJ({"project": {**_STR, "description": "TISC 项目名/路径，或本地 .splcproj/项目目录"}},
               ["project"]), True,
          lambda a: ps_com("safety-files", project=a["project"]), "Safety"),
    _tool("tc_safety_target_info",
          "只读解析 Safety 项目属性和 TargetSystemConfig.xml，包括目标类型、语言、作者、内部项目名、"
          "FSOE 地址及已保存的目标映射字段；不与硬件进行一致性验证。",
          _OBJ({"project": {**_STR, "description": "TISC 项目名/路径，或本地 .splcproj/项目目录"}},
               ["project"]), True,
          lambda a: ps_com("safety-target-info", project=a["project"]), "Safety"),
    _tool("tc_safety_aliases",
          "只读解析 .sds Alias Device：所属 Group、SDS ID、标准/安全类型、通道数据以及文件中已有的映射字段；"
          "不验证安全地址唯一性或物理硬件对应关系。",
          _OBJ({
              "project": {**_STR, "description": "TISC 项目名/路径，或本地 .splcproj/项目目录"},
              "group": {**_STR, "description": "可选 TwinSAFE Group 名称"},
          }, ["project"]), True,
          lambda a: ps_com("safety-aliases", project=a["project"], group=str(a.get("group") or "")),
          "Safety"),
    _tool("tc_safety_application",
          "只读解析图形 SAL 或 Safety C Group 结构：Network、TwinSAFE FB、输入/输出/参数端口、"
          "FB 连线、变量引用、Group 配置及源文件清单。仅做结构回读，不验证 Safety 语义、CRC 或硬件映射。",
          _OBJ({
              "project": {**_STR, "description": "TISC 项目名/路径，或本地 .splcproj/项目目录"},
              "group": {**_STR, "description": "可选 TwinSAFE Group 名称"},
          }, ["project"]), True,
          lambda a: ps_com("safety-application", project=a["project"], group=str(a.get("group") or "")),
          "Safety"),
    _tool("tc_safety_logic_check",
          "只读检查已保存 TwinSAFE 图形结构的一致性：重复 FB 执行顺序、重复 SDS ID、变量到 Alias Device "
          "以及 FB 端口连线的悬空引用。它不是 TwinSAFE Verify，不能证明安全完整性或代替 CRC/硬件验收。",
          _OBJ({
              "project": {**_STR, "description": "TISC 项目名/路径，或本地 .splcproj/项目目录"},
              "group": {**_STR, "description": "可选 TwinSAFE Group 名称"},
          }, ["project"]), True,
          lambda a: ps_com("safety-logic-check", project=a["project"], group=str(a.get("group") or "")),
          "Safety"),
    _tool("tc_safety_validate",
          "只读检查 TISC 结构，或预检一个 .splcproj/.tfzip 模板文件。只做结构校验，"
          "不能替代安全逻辑验证、风险评估、CRC 验收和现场调试。",
          _OBJ({"source": {**_STR, "description": "可选 Safety 模板完整路径"}}),
          True, lambda a: ps_com("safety-validate", source=str(a.get("source") or "")),
          "Safety"),
    _tool("tc_safety_import",
          "从官方支持的 .splcproj/.tfzip 模板导入 Safety 项目。默认仅预览；apply=true 还必须"
          " acknowledge_safety_review=true。move 模式另需 confirm_source_move=true。"
          "不会下载到 Safety Target、不会激活配置或接受 CRC。",
          _OBJ({
              "source": {**_STR, "description": "现有 .splcproj 或 .tfzip 完整路径"},
              "name": {**_STR, "description": "copy/move 模式项目名；reference 模式按官方契约忽略"},
              "mode": {"type": "string", "enum": ["copy", "move", "reference"], "default": "copy"},
              "apply": {"type": "boolean"},
              "confirm_source_move": {"type": "boolean"},
              "acknowledge_safety_review": {"type": "boolean"},
          }, ["source"]), False,
          lambda a: ps_com("safety-import", source=a["source"], name=str(a.get("name") or ""),
                           mode=str(a.get("mode") or "copy"), apply=bool(a.get("apply", False)),
                           confirm_source_move=bool(a.get("confirm_source_move", False)),
                           acknowledge_safety_review=bool(a.get("acknowledge_safety_review", False))),
          "Safety", "safety"),
    _tool("tc_safety_create",
          "从本机已安装的 Beckhoff Safety 模板创建项目：默认使用界面中的 Preconfigured Inputs（ErrAck + Run），"
          "也可选择仅 ErrAck 或 Empty；自动选择最新模板、生成 GUID、"
          "填入目标/语言/作者，在隔离临时目录实例化后以 copy 模式导入并清理临时文件。"
          "hardware 使用 HSafetyPLC/GraphicalEditor；twincat-safety-plc 使用 TSafetyPLC/SafetyC。"
          "默认仅预览，不下载、不激活、不接受 CRC。",
          _OBJ({
              "name": {**_STR, "description": "Safety 项目名"},
              "target": {"type": "string", "enum": ["hardware", "twincat-safety-plc"], "default": "hardware"},
              "template": {"type": "string", "enum": ["empty", "preconfigured-errack", "preconfigured-inputs"], "default": "preconfigured-inputs"},
              "author": {**_STR, "description": "安全项目作者，默认 TwinCAT Agent"},
              "internal_project_name": {**_STR, "description": "内部项目名，默认同 name"},
              "apply": {"type": "boolean"},
              "acknowledge_safety_review": {"type": "boolean"},
          }, ["name"]), False,
          lambda a: ps_com("safety-create", name=a["name"], target=str(a.get("target") or "hardware"),
                           template=str(a.get("template") or "preconfigured-inputs"),
                           author=str(a.get("author") or "TwinCAT Agent"),
                           internal_project_name=str(a.get("internal_project_name") or ""),
                           apply=bool(a.get("apply", False)),
                           acknowledge_safety_review=bool(a.get("acknowledge_safety_review", False))),
          "Safety", "safety"),
    _tool("tc_safety_export",
          "将当前 .tsproj 精确引用的 Safety 项目目录归档为 .tfzip，并回读 ZIP 目录确认包含 .splcproj。默认仅预览；"
          "apply=true 需要 Safety 审查确认，输出存在时还需 overwrite=true。",
          _OBJ({
              "project": {**_STR, "description": "精确项目名或 TISC 路径"},
              "output_file": {**_STR, "description": "目标 .tfzip 完整路径；父目录必须存在"},
              "overwrite": {"type": "boolean"}, "apply": {"type": "boolean"},
              "acknowledge_safety_review": {"type": "boolean"},
          }, ["project", "output_file"]), False,
          lambda a: ps_com("safety-export", project=a["project"], output_file=a["output_file"],
                           overwrite=bool(a.get("overwrite", False)), apply=bool(a.get("apply", False)),
                           acknowledge_safety_review=bool(a.get("acknowledge_safety_review", False))),
          "Safety", "safety"),
    _tool("tc_safety_remove",
          "仅当用户明确要求保留底层文件时，精确预览或从 TISC 移除一个 Safety 项目；一般的‘删除项目’"
          "应优先使用 tc_safety_delete。apply=true 必须同时确认安全审查并逐字"
          "提供 confirm_project_name；执行前应先导出或备份。此操作只从配置树移除项目，"
          "不会删除底层 .splcproj 文件或工程目录。",
          _OBJ({
              "project": {**_STR, "description": "精确项目名或 TISC 路径"},
              "apply": {"type": "boolean"},
              "confirm_project_name": {**_STR, "description": "apply 时必须与实际项目名完全一致"},
              "acknowledge_safety_review": {"type": "boolean"},
          }, ["project"]), False,
          lambda a: ps_com("safety-remove", project=a["project"], apply=bool(a.get("apply", False)),
                           confirm_project_name=str(a.get("confirm_project_name") or ""),
                           acknowledge_safety_review=bool(a.get("acknowledge_safety_review", False))),
          "Safety", "safety"),
    _tool("tc_safety_delete",
          "删除 Safety 项目的默认工具：先导出并验证 .tfzip 备份，再从 TISC 移除，最后删除当前解决方案目录内的"
          "底层工程目录；也支持已从 TISC 移除后的孤立 .splcproj/项目目录。只有明确保留文件时才使用"
          " tc_safety_remove。默认仅预览；拒绝删除解决方案外部目录。apply=true 必须逐字确认项目名、确认删除文件并"
          "确认 Safety 审查。",
          _OBJ({
              "project": {**_STR, "description": "TISC 项目名/路径，或解决方案内孤立的 .splcproj/项目目录"},
              "backup_file": {**_STR, "description": "可选 .tfzip 路径；默认保存到 .TwinCATAgent/SafetyBackups"},
              "apply": {"type": "boolean"},
              "confirm_project_name": {**_STR, "description": "apply 时必须与实际项目名完全一致"},
              "confirm_delete_files": {"type": "boolean"},
              "acknowledge_safety_review": {"type": "boolean"},
          }, ["project"]), False,
          lambda a: ps_com("safety-delete", project=a["project"],
                           backup_file=str(a.get("backup_file") or ""),
                           apply=bool(a.get("apply", False)),
                           confirm_project_name=str(a.get("confirm_project_name") or ""),
                           confirm_delete_files=bool(a.get("confirm_delete_files", False)),
                           acknowledge_safety_review=bool(a.get("acknowledge_safety_review", False))),
          "Safety", "safety"),
    _tool("tc_target_show", "读取当前 TwinCAT 系统项目绑定的目标 AMS NetId。",
          _OBJ(), True, lambda a: ps_com("target-show"), "目标"),
    _tool("tc_target_routes",
          "只读列出本机已配置的 TwinCAT 静态路由，可用于按设备名、IP 或 AMS NetId 选择已有目标。",
          _OBJ(), True, lambda a: list_static_routes(), "目标"),
    _tool("tc_target_set",
          "把当前系统项目切换到一个已有路由名/IP，或直接指定六段 AMS NetId；不新增、不删除系统路由。",
          _OBJ({"target": {**_STR,
                           "description": "路由名、IP 或 AMS NetId，例如 CB-3BD8F0"}}, ["target"]),
          False, _target_set, "目标", "target"),
    _tool("tc_io_structure",
          "只读导出当前系统项目的 EtherCAT I/O 树结构；不扫描硬件、不写文件。",
          _OBJ({"master": {**_STR, "description": "可选：只查看指定 I/O 主站的完整名称"}}),
          True, lambda a: _io("export", a), "I/O"),
    _tool("tc_io_masters", "列出当前工程真实配置的 I/O 主站（按 Automation Interface ItemType 2 识别）。",
          _OBJ(), True, lambda a: ps_com("io-masters"), "I/O"),
    _tool("tc_io_master_info", "读取指定 I/O 主站的完整路径、类型和直接子节点；多主站时必须指定完整名称。",
          _OBJ({"master": {**_STR, "description": "I/O 主站完整名称，可由 tc_io_masters 获取"}}),
          True, lambda a: ps_com("io-master-info", master=str(a.get("master") or "")), "I/O"),
    _tool("tc_io_manifest_check",
          "离线检查 EtherCAT 设备清单的结构、父子关系和创建顺序，不连接 XAE。"
          "可传 JSON 文件路径，或直接传 configuration 对象。",
          _OBJ({"manifest": {**_STR, "description": "设备清单 JSON 完整路径"},
                "configuration": _IO_CONFIGURATION}),
          True, lambda a: _io("check-manifest", a), "I/O"),
    _tool("tc_io_esi_check",
          "只读检查设备清单所需产品是否能在本机 ESI XML 中找到；不会安装 ESI。",
          _OBJ({"manifest": {**_STR, "description": "设备清单 JSON 完整路径"},
                "configuration": _IO_CONFIGURATION}),
          True, lambda a: _io("esi-check", a), "I/O"),
    _tool("tc_io_validate",
          "只读对比当前 EtherCAT I/O 树与设备清单，报告缺失设备和顺序差异。",
          _OBJ({"manifest": {**_STR, "description": "设备清单 JSON 完整路径"},
                "configuration": _IO_CONFIGURATION}),
          True, lambda a: _io("validate", a), "I/O"),
    _tool("tc_io_create",
          "按 JSON 清单离线创建 EtherCAT 主站和端子；不扫描、不激活、不重启。"
          "默认拒绝在非空 I/O 树中创建，确认后可设置 allow_existing。",
          _OBJ({"manifest": {**_STR, "description": "设备清单 JSON 完整路径"},
                "configuration": {
                    **_IO_CONFIGURATION,
                    "description": "从 EPLAN/PDF 提取后可直接传入的内联设备清单"},
                "allow_existing": {
                    "type": "boolean",
                    "description": "已有 I/O 树时允许只补齐清单中缺失项，默认 false"}}),
          False, lambda a: _io("create", a), "I/O", "project"),
    _tool("tc_io_remove",
          "删除设备清单中列出的 EtherCAT 从站，按逆序删除并保留 EtherCAT 主站；"
          "不激活、不重启，必须确认。",
          _OBJ({"manifest": {**_STR, "description": "设备清单 JSON 完整路径"},
                "configuration": _IO_CONFIGURATION}),
          False, lambda a: _io("remove", a), "I/O", "project"),
    _tool("tc_io_remove_master",
          "删除指定 I/O 主站。默认只允许删除没有物理从站的空主站；有从站时必须显式 allow_with_children。此操作不可撤销并需要确认。",
          _OBJ({"master": {**_STR, "description": "I/O 主站完整名称"},
                "allow_with_children": {"type": "boolean", "description": "允许连同主站删除其物理子设备，默认 false"}}, ["master"]),
          False, lambda a: _io("remove-master", a), "I/O", "project"),
    _tool("tc_io_export",
          "把当前 EtherCAT I/O 树导出为 JSON 设备清单文件；不修改系统项目。",
          _OBJ({"output": {**_STR, "description": "输出 JSON 完整路径"},
                "master": {**_STR, "description": "存在多个主站时必填，使用 tc_io_structure 返回的完整名称"}},
               ["output"]),
          False, lambda a: _io("export", a), "I/O", "file"),
    _tool("tc_scan_devices",
          "仅当用户明确要求硬件扫描时，扫描当前目标的物理 EtherCAT 设备并加入系统项目。"
          "不要用它替代 EPLAN/JSON 清单离线组态。目标必须已经处于 Config 模式；"
          "本工具不会自动切换模式、激活配置或重启 TwinCAT。仅清理由本轮扫描新建且无从站的适配器，"
          "不会删除已有设备。",
          _OBJ(), False, _scan_devices,
          "I/O", "runtime"),
    _tool("plc_coding_profile", "读取当前解决方案的项目级 PLC 生成规则。新建或大改 PLC 代码前先读取；规则已注入系统提示，本工具用于显式核对。",
          _OBJ(), True, _coding_profile_read, "代码生成"),
    _tool("fblib_find", "按功能意图搜索经过验证的 PLC 功能块模板。代码质量门禁启用时，新建任何 FB 前必须先调用；有匹配时优先 fblib_add。",
          _OBJ({"intent": {**_STR, "description": "要实现的功能，例如 单轴回零、TCP客户端、气缸控制"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["intent"]),
          True, _fblib_find, "代码生成"),
    _tool("plc_generate", "按项目 coding profile 生成标准 FB 候选代码，不写入 XAE。事务型自动包含状态四件套、超时和 CASE；循环服务型使用 bEnable 周期骨架。",
          _OBJ({"name": {**_STR, "description": "FB 名，缺少 FB_ 时自动补齐"},
                "purpose": {**_STR, "description": "一句话功能职责"},
                "author": {**_STR, "description": "可选；默认使用项目 coding profile"},
                "mode": {"type": "string", "enum": ["transaction", "service"],
                         "description": "有完成概念用 transaction；持续周期运行用 service"}},
               ["name", "purpose", "mode"]),
          True, _generate_standard_fb, "代码生成"),
    _tool("plc_list", "列出当前 PLC 项目里的所有对象(POU/DUT/GVL/接口)。大型项目找单个对象优先用 plc_find。",
          _OBJ(), True, lambda a: ps_com("list"), "代码"),
    _tool("plc_find", "按名称快速查找 POU/FB/DUT/GVL/接口及成员；只读名称索引，不读取全项目代码。"
          "已知或大概知道 FB 名时必须优先用本工具，不要先展开 plc_structure。",
          _OBJ({"query": {**_STR, "description": "完整名或名称片段，如 FB_Motor"},
                "folder": {"type": "string", "enum": ["", "POUs", "DUTs", "GVLs", "Interfaces", "VISUs"],
                           "description": "可选对象文件夹过滤"},
                "limit": {"type": "integer", "description": "最多返回条数，默认30"},
                "include_members": {"type": "boolean", "description": "是否同时匹配方法/属性名，默认true"}},
               ["query"]),
          True, lambda a: find_pou(
              a["query"], a.get("folder", ""), int(a.get("limit") or 30),
              bool(a.get("include_members", True))), "代码"),
    _tool("plc_structure", "返回有界 PLC 项目结构摘要。大型项目默认每类最多80项且不展开成员；"
          "找某个 FB 请使用 plc_find。",
          _OBJ({"limit_per_folder": {"type": "integer", "description": "每类最多返回数量，默认80，最大200"},
                "include_members": {"type": "boolean", "description": "是否展开方法/属性名，默认false"}}),
          True, lambda a: ps_com(
              "structure", limit_per_folder=min(200, max(1, int(a.get("limit_per_folder") or 80))),
              include_members=bool(a.get("include_members", False))), "代码"),
    _tool("plc_tree", "读取当前 PLC 的实时树路径，包含空文件夹；创建对象或继续创建子文件夹前，优先用它获取精确 parent_path。",
          _OBJ({"max_nodes": {"type": "integer", "minimum": 1, "maximum": 5000}}),
          True, lambda a: ps_com(
              "solution-tree", max_nodes=min(5000, max(1, int(a.get("max_nodes") or 2500)))), "代码"),
    _tool("plc_snapshots", "列出当前解决方案已有的 PLC 程序快照。",
          _OBJ({"limit": {"type": "integer", "description": "最多返回数量，默认20"}}),
          True, lambda a: plc_versions.list_snapshots(int(a.get("limit") or 20)), "版本"),
    _tool("plc_changed", "把当前 PLC 程序与项目本地快照比较；只传输哈希和变化摘要，"
          "适合大型项目快速确认哪些 POU/方法发生了变化。",
          _OBJ({"snapshot": {**_STR, "description": "快照名称或文件名，默认latest"},
                "name": {**_STR, "description": "可选：只检查指定对象名/路径片段"},
                "limit": {"type": "integer", "description": "最多返回变化对象数，默认100"}}),
          True, lambda a: plc_versions.changed(
              a.get("snapshot") or "latest", a.get("name") or "",
              int(a.get("limit") or 100)), "版本"),
    _tool("plc_diff", "把当前 PLC 程序与快照做方法/声明/实现级统一差异比较；"
          "先比较哈希，只读取真正变化的对象，并限制返回正文。",
          _OBJ({"snapshot": {**_STR, "description": "快照名称或文件名，默认latest"},
                "name": {**_STR, "description": "可选：只比较指定对象名/路径片段"},
                "context": {"type": "integer", "description": "差异上下文行数，默认3"},
                "max_hunks": {"type": "integer", "description": "最多差异块，默认50"},
                "max_chars": {"type": "integer", "description": "差异正文最大字符数，默认12000"}}),
          True, lambda a: plc_versions.diff(
              a.get("snapshot") or "latest", a.get("name") or "",
              int(a.get("context") or 3), int(a.get("max_hunks") or 50),
              int(a.get("max_chars") or 12000)), "版本"),
    _tool("plc_read", "读取代码对象，不接受项目名或 .plcproj；工程属性用 tc_project_info，对象路径用 plc_find。默认只返回对象本体和成员名称，不加载所有成员正文；"
          "读取成员时传 method；Action/Transition 也支持，可用 member_type 明确类型。"
          "大型/分文件夹项目应传 plc_find 返回的 POU path 直接定位。",
          _OBJ({"name": {**_STR, "description": "对象名，如 MAIN"},
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径；嵌套对象建议必传"},
                "source_path": {**_STR, "description": "可选：plc_read_smart/索引返回的已保存源码路径；仅用于读取，不是 COM 写入路径"},
                "area": {"type": "string", "enum": ["all", "declaration", "implementation", "members"],
                         "description": "读取区域，默认all"},
                "method": {**_STR, "description": "可选成员路径，如 Start 或 Value.Get"},
                "member_type": {"type": "string",
                                "enum": ["method", "action", "transition", "property", "propget", "propset"],
                                "description": "可选成员类型；用于区分 action/method 并改善错误提示"},
                "include_member_code": {"type": "boolean", "description": "是否附带所有成员正文；大型FB慎用，默认false"},
                "structure_baseline": {"type": "boolean", "description": "只读获取父对象和全部成员实时哈希基线，用于未保存状态下安全删除/重命名成员；必须指定父对象path，不使用method/direct"},
                "document_baseline": {"type": "boolean", "description": "只读获取单文档保存基线：必须指定父对象name/path且父文档已打开；返回document_baseline供plc_save_document按次审批使用，不保存文件"},
                "start_line": {"type": "integer", "description": "起始行，默认1"},
                "max_lines": {"type": "integer", "minimum": 0, "description": "每个区域返回行数；默认0完整读取，正数为显式分页"},
                "direct": {"type": "boolean", "description": "显式优先使用已配置的 System Manager WebSocket；失败保留原因并回退 COM"},
                "protocol_project_id": {"description": "System Manager projectList 返回的协议项目 ID；多项目时必填"},
                "tid": {"type": "integer", "description": "可选 System Manager tree item ID"},
                "tname": {**_STR, "description": "可选 System Manager tname；未传时使用 path"}},
               ["name"]),
          True, _plc_read, "代码"),
    _tool("plc_read_fast", "批量读取 1~16 个 PLC 对象或成员；默认一次 SQLite 索引同步后读取已保存源码。"
          "tree_path 自动选择一次 Helper/DTE 实时读取，source_path 选择索引读取；不提供路径时才由 live 选择。支持区域、分页和 known_hashes；哈希未变化的区域不重复返回源码。"
          "读取整个 FB/Program 的多个方法或 Action 时必须用它合并请求，避免逐对象连续调用 plc_read；"
          "完整成员优先设 max_lines=500、max_total_chars=20000。",
          _OBJ({
              "requests": {"type": "array", "minItems": 1, "maxItems": 16,
                           "items": {"type": "object", "properties": {
                               "id": _STR, "name": _STR,
                               "path": {**_STR, "description": "plc_find 返回的精确树路径"},
                               "source_path": {**_STR, "description": "可选：已保存索引路径；仅用于读取，不是 COM 写入路径"},
                               "area": {"type": "string",
                                        "enum": ["all", "declaration", "implementation", "members"]},
                               "method": {**_STR, "description": "方法/Action/Property 成员路径"},
                               "member_type": {"type": "string",
                                               "enum": ["method", "action", "transition", "property", "propget", "propset"]},
                               "start_line": {"type": "integer", "minimum": 1},
                               "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
                               "known_hashes": {"type": "object", "properties": {
                                   "declaration": _STR, "implementation": _STR,
                               }},
                               "tid": {"type": "integer"},
                               "tname": _STR,
                           }, "required": ["name"]}},
              "live": {"type": "boolean", "description": "true 时强制走一次 XAE COM 批读取；默认读取已保存 SQLite 索引"},
              "direct": {"type": "boolean", "description": "显式优先使用已配置的 System Manager 批量只读；失败不伪造成功"},
              "protocol_project_id": {"description": "System Manager projectList 返回的协议项目 ID"},
              "timeout_s": {"type": "number", "minimum": 0.1, "maximum": 300},
              "max_total_chars": {"type": "integer", "minimum": 1000,
                                  "maximum": 20000,
                                  "description": "整批源码返回上限，默认/最大20000；超出部分会标出 omitted_chars，避免上下文首尾拼接截断"},
          }, ["requests"]), True, _plc_read_fast, "代码"),
    _tool("plc_read_smart", "读取代码对象或 FB 成员，不接受项目名/.plcproj；工程属性用 tc_project_info。默认从 .plcproj 精确索引的"
          "磁盘 XML 毫秒级读取；验证、写后检查或需要未保存的 XAE 编辑时传 live=true 强制 COM。"
          "磁盘定位失败会自动回退实时 COM，并返回 source、哈希、路径和耗时。",
          _OBJ({"name": {**_STR, "description": "对象名，如 MAIN 或 FB_Motor"},
                "path": {**_STR, "description": "可选完整树路径，用于消除同名对象歧义"},
                "source_path": {**_STR, "description": "可选已保存索引路径；仅用于磁盘读取，不是 COM 写入路径"},
                "area": {"type": "string", "enum": ["all", "declaration", "implementation"],
                         "description": "读取区域，默认 all"},
                "method": {**_STR, "description": "可选内部成员路径，如 Start、Value.Get"},
                "member_type": {"type": "string",
                                "enum": ["method", "action", "transition", "property", "propget", "propset"]},
                "live": {"type": "boolean",
                         "description": "true=强制读取当前 XAE 实时内容；默认 false 使用磁盘极速读取"},
                "direct": {"type": "boolean", "description": "显式优先使用已配置的 System Manager WebSocket；失败回退 COM"},
                "protocol_project_id": {"description": "System Manager projectList 返回的协议项目 ID"},
                "tid": {"type": "integer"}, "tname": {**_STR}},
               ["name"]),
          True, _plc_read_smart, "代码"),
    _tool("plc_source_index", "同步并显示当前解决方案的已保存 PLC 源码 SQLite 索引状态。"
          "仅解析发生变化的 .TcPOU/.TcGVL/.TcDUT 文件；不读取 XAE 未保存编辑内容。",
          _OBJ({"solution": {**_STR, "description": "可选 .sln 或项目目录；默认当前 XAE 解决方案"}}),
          True, _plc_source_index_status, "代码"),
    _tool("plc_source_catalog", "一次返回当前项目的紧凑对象目录（名称、类型、精确路径、成员名称），不返回源码正文。"
          "用于全项目概览或批量阅读前筛选对象，替代反复 plc_find/plc_structure/plc_list。默认来自已保存 SQLite 索引。",
          _OBJ({"solution": {**_STR, "description": "可选 .sln 或项目目录；默认当前 XAE 解决方案"},
                "query": {**_STR, "description": "可选名称/路径/成员名包含筛选"},
                "kind": {**_STR, "description": "可选对象类型精确筛选，如 POU、GVL、DUT"},
                "include_members": {"type": "boolean", "description": "是否带成员名称，默认true"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "description": "最多对象数，默认200"}}),
          True, _plc_source_catalog, "代码"),
    _tool("plc_read_current", "读取 XAE 当前活动 PLC 文档的完整实时内容，无需对象名、搜索或"
          "遍历项目树。返回当前对象/成员、保存状态、精确路径和区域哈希；用于读取正在编辑的代码。",
          _OBJ({"area": {"type": "string", "enum": ["all", "declaration", "implementation"],
                         "description": "读取区域，默认 all"},
                "member_type": {"type": "string",
                                "enum": ["method", "action", "transition", "property", "propget", "propset"]}},
               ), True, _plc_read_current, "代码"),
    _tool("plc_dirty_current", "仅比较当前 XAE 活动 PLC 文档的实时内容与对应磁盘文件，"
          "判断当前编辑是否未保存。不会扫描 PLC 项目或读取其他对象。",
          _OBJ({"area": {"type": "string", "enum": ["all", "declaration", "implementation"],
                         "description": "比较区域，默认 all"},
                "member_type": {"type": "string",
                                "enum": ["method", "action", "transition", "property", "propget", "propset"]}},
               ), True, _plc_dirty_current, "代码"),
    _tool("plc_syntax_templates", "只读查询 XAE 声明/实现分离的语法模板，含 FB、Program、DUT、接口、方法、属性及 Get/Set；不是编译通过证明。",
          _OBJ({"kind": _STR}), True,
          lambda a: syntax_templates(a.get('kind', '')), "代码生成"),
    _tool("plc_editor_state", "只读查询一个 PLC 工程的 XAE Login/Logout 状态，不查询或改变 PLC Runtime Run/Stop。多 PLC 必须指定实际 runtime；未知登录状态不会猜测。",
          _OBJ({"runtime": {**_STR, "description": "实际 PLC runtime 名称；多 PLC 时必须明确选择"}}),
          True, _plc_editor_state, "代码"),
    _tool("plc_build_status", "只读核对当前绑定 XAE 的解决方案/PLC 项目、PLC 登录状态、Build/Rebuild 实际命令可用性和构建占用。"
          "成功时签发短时 build_plan_token；令牌绑定 PID、解决方案、项目范围、实际 action 和状态指纹，不能由调用方伪造 expected_state。",
          _OBJ({"action": {"type": "string", "enum": ["build", "rebuild"],
                           "description": "可选；只返回该实际命令的审批令牌"},
                "runtime": {**_STR, "description": "可选 PLC runtime 名称；仅用于读取登录状态，不登录/登出/停止"}}),
          True, capture_build_state, "代码"),
    _tool("plc_build", "执行已获用户明确审批且与最新只读状态精确绑定的 PLC Build/Rebuild；"
          "必须先用 plc_build_status 获取令牌。构建失败、状态冲突或结果不完整都不会自动改代码、Logout、Stop、Start、Activate 或重试。",
          _OBJ({"action": {"type": "string", "enum": ["build", "rebuild"]},
                "build_plan_token": {**_STR, "description": "原样使用 plc_build_status 为同一 action 签发的短时令牌"}},
               ["action", "build_plan_token"]),
          False, execute_plc_build, "代码", "build"),
    _tool("plc_diagnostics", "只读恢复当前绑定 XAE 的错误列表；不编译、不改焦点、不写程序。诊断缺失时优先使用；结果不证明本轮代码编译通过。",
          _OBJ({"direct": {"type": "boolean", "description": "显式从已配置 System Manager 读取 sm.plccompilermsg；不触发 Build"},
                "protocol_project_id": {"description": "System Manager projectList 返回的协议项目 ID"},
                "tid": {"type": "integer"}, "tname": {**_STR}, "level": {"type": "integer", "minimum": 0}},
               ), True, _plc_diagnostics, "代码"),
    _tool("plc_preflight", "写入前只读审核整批完整候选对象。按项目路径叠加候选声明，检查语句、类型、数组和参数；未知语义不放行。不写程序、不编译、不启动 XAE。",
          _OBJ({'candidates': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                'items': _OBJ({'name': _STR, 'path': _STR, 'declaration': _STR, 'implementation': _STR},
                              ['name', 'path', 'declaration', 'implementation'])}}, ['candidates']),
          True, _plc_preflight, "代码"),
    _tool("plc_verify", "执行分层验证：真实编译 → Agent 静态分析 → 可选在线变量断言。"
          "源码检查失败时跳过在线断言；在线值通过不证明运行的是本次构建。"
          "source_verified/runtime_values_verified 分别报告，未确认构建身份时在线验证为 incomplete，不自动下载或启动。",
          _OBJ({
              "action": {"type": "string", "enum": ["build", "rebuild"]},
              "build_plan_token": {**_STR, "description": "原样使用 plc_build_status 为同一 action 签发的短时令牌"},
              "max_complexity": {"type": "integer", "minimum": 1,
                                 "description": "静态分析复杂度阈值，默认20"},
              "rule_severities": _STATIC_RULE_SEVERITIES,
              "runtime": {**_STR, "description": "可选 PLC runtime 名称"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "symbols": {"type": "array", "maxItems": 32,
                          "description": "可选在线断言；省略时只验证编译和静态分析",
                          "items": {"type": "object", "properties": {
                              "name": _PLC_SYMBOL_SCHEMA,
                              "type": {"type": "string", "enum": sorted(_PLC_VALUE_TYPES)},
                              "expected": {},
                              "tolerance": {"type": "number", "minimum": 0},
                          }, "required": ["name", "type", "expected"]}},
          }, ["action", "build_plan_token"]), False, _plc_verify_workflow, "代码", "build"),
    _tool("plc_libraries", "列出当前 PLC 项目引用的库。",
          _OBJ(), True, lambda a: ps_com("lib-list"), "代码"),
    _tool("plc_review", "只读审查指定 POU 的当前完整代码：声明分区、变量注释/范围、事务 FB 合约、状态机及子 FB 调用区。",
          _OBJ({"name": _STR,
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径"}}, ["name"]),
          True, _review_existing_pou, "代码"),
    _tool("plc_vars", "列出 PLC 变量，可按 POU 名过滤；返回变量名、类型和作用域。",
          _OBJ({"pou": {**_STR, "description": "可选 POU 名，如 MAIN；留空=全项目"}}),
          True, lambda a: list_variables(a.get("pou") or None), "代码"),
    _tool("plc_search", "优先从项目 SQLite 索引搜索已保存 PLC 代码，只返回命中行；索引不可用时回退 XAE COM。"
          "未保存编辑请用 plc_read_current 或 live=true 验证。若只是按名称找 FB/方法，请改用更快的 plc_find。",
          _OBJ({"pattern": {**_STR, "description": "搜索文字或正则表达式"},
                "regex": {"type": "boolean", "description": "是否按正则表达式搜索"},
                "case_sensitive": {"type": "boolean", "description": "是否区分大小写"},
                "pou": {**_STR, "description": "可选：只搜索这个 POU/FB"},
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径；与 pou 配合使用"},
                "max_results": {"type": "integer", "description": "最多返回命中数，默认50，最大500"}},
               ["pattern"]),
          True, _plc_search_indexed, "代码"),

    # ---- 只读:倍福官方文档全文搜索(写库函数块代码前先查,别凭记忆) ----
    _tool("docs_search",
          "全文搜索倍福 InfoSys 官方文档(145k 页)。写/改用到库函数块的 PLC 代码前,"
          "先搜确认其正确的输入/输出/方法名,不要凭记忆。返回按相关性排序的标题/路径/片段。",
          _OBJ({"query": {**_STR, "description": "关键词，如 'FB_SocketAccept' 或 'Tc2_TcpIp FB_SocketReceive'"},
                "limit": {"type": "integer", "description": "返回条数，默认 8，最大 50"},
                "product": {**_STR, "description": "可选：按产品字段缩小范围，如 'TE2000' 或 'TF5xxx'"},
                "path_prefix": {**_STR, "description": "可选：按文档 path 前缀缩小范围，如 'content/TwinCAT-Agent-Supplement/'"}}, ["query"]),
          True, lambda a: docsearch.search(
              a["query"], int(a.get("limit") or 8),
              product=a.get("product") or None,
              path_prefix=a.get("path_prefix") or None), "文档"),
    _tool("docs_read",
          "按 docs_search 返回的 path 读取某篇倍福文档的正文,看清库函数块的完整 API 定义。",
          _OBJ({"path": {**_STR, "description": "docs_search 结果里的 path"}}, ["path"]),
          True, lambda a: docsearch.read(a["path"]), "文档"),

    # ---- 写:项目管理 / PLCopen 导入导出(需审批) ----
    _tool("plc_create_project",
          "在当前 TwinCAT 系统项目的 TIPC 下新建 PLC 项目；无解决方案时先用 tc_create_solution。",
          _OBJ({"name": {**_STR, "description": "PLC 项目名，默认 PLC1"},
                "template": {**_STR, "description": "模板名或模板路径，默认 Standard PLC Template"}}),
          False, lambda a: ps_com(
              "create-plc-project", name=a.get("name") or "PLC1",
              template=a.get("template") or "Standard PLC Template"), "项目"),
    _tool("plc_remove_project",
          "仅从当前 XAE 的 TIPC 树移除指定 PLC 项目，保留磁盘上的 .plcproj 和项目目录。",
          _OBJ({"name": {**_STR, "description": "要从 XAE 移除的 PLC 项目名"}}, ["name"]),
          False, lambda a: ps_com("remove-plc-project", name=a["name"]),
          "项目", "project"),
    _tool("plc_delete_project",
          "真正删除 PLC 项目：从 TIPC 移除并删除 .tsproj 中 PrjFilePath 指向的本地独占项目目录；路径不安全时拒绝。不可撤销。",
          _OBJ({"name": {**_STR, "description": "要删除的 PLC 项目名"}}, ["name"]),
          False, lambda a: ps_com("delete-plc-project", name=a["name"]),
          "项目", "file"),
    _tool("tc_open", "保存并关闭当前解决方案，然后在同一个 XAE 实例中打开指定 .sln。",
          _OBJ({"path": {**_STR, "description": ".sln 文件完整路径"}}, ["path"]),
          False, lambda a: ps_com("open-solution", path=a["path"], timeout=90.0),
          "项目", "project"),
    _tool("tc_solution_templates", "列出可用于在空白 XAE 创建解决方案的已安装模板；按实际模板名称选择。",
          _OBJ(), True,
          lambda a: __import__('tc_template.solution_creation', fromlist=['template_catalog']).template_catalog(), '项目'),
    _tool("tc_create_solution", "在当前已连接且没有工程的 XAE 中从模板创建并打开新 TwinCAT 解决方案；回读确认同一 PID。不会覆盖目录。",
          _OBJ({'name': _STR, 'output_dir': {**_STR, 'description': '用户指定的绝对父目录'},
                'template': {**_STR, 'description': 'tc_solution_templates 返回的模板名称'}},
               ['name', 'output_dir', 'template']), False,
          lambda a: __import__('tc_template.solution_creation', fromlist=['create_solution']).create_solution(
              a['name'], a['output_dir'], a['template']), '项目', 'project'),
    _tool("tc_close", "保存全部文件并关闭当前解决方案，不退出 XAE。",
          _OBJ(), False, lambda a: ps_com("close-solution"),
          "项目", "project"),
    _tool("plc_import_plcopen", "将 PLCopen XML 文件导入当前 PLC 项目。",
          _OBJ({"file": {**_STR, "description": "PLCopen XML 文件完整路径"},
                "options": {"type": "integer", "description": "导入选项，默认 0"}}, ["file"]),
          False, lambda a: ps_com(
              "import-plcopen", file=a["file"], options=int(a.get("options") or 0)),
          "导入导出", "project"),
    _tool("plc_export_plcopen", "把指定 PLC 对象导出为 PLCopen XML 文件。",
          _OBJ({"file": {**_STR, "description": "输出 XML 文件完整路径"},
                "pous": {"type": "array", "items": _STR,
                         "description": "要导出的对象名数组，如 [\"MAIN\",\"FB_Motor\"]"}},
               ["file", "pous"]),
          False, lambda a: ps_com(
              "export-plcopen", file=a["file"], pous=list(a["pous"])),
          "导入导出", "file"),

    # ---- 写:库管理(需审批) ----
    _tool("plc_lib_add", "给当前 PLC 项目添加一个库引用(References)。",
          _OBJ({"name": {**_STR, "description": "库名，如 Tc2_Standard"},
                "version": {**_STR, "description": "版本，默认 * 取最新"},
                "distributor": {**_STR, "description": "发布者，可留空"}}, ["name"]),
          False, lambda a: ps_com("lib-add", name=a["name"], version=a.get("version", "*"),
                                  distributor=a.get("distributor", "")), "库"),
    _tool("plc_lib_remove", "从当前 PLC 项目移除一个库引用。",
          _OBJ({"name": _STR, "version": _STR, "distributor": _STR}, ["name"]),
          False, lambda a: ps_com("lib-remove", name=a["name"], version=a.get("version", ""),
                                  distributor=a.get("distributor", "")), "库"),
    _tool("plc_placeholder_add", "给当前 PLC 项目添加一个占位符库引用(placeholder)。",
          _OBJ({"name": _STR,
                "default_lib": {**_STR, "description": "占位符解析到的默认库名"},
                "default_version": {**_STR, "description": "默认版本，默认 *"},
                "default_distributor": _STR}, ["name"]),
          False, lambda a: ps_com("placeholder-add", name=a["name"],
                                  default_lib=a.get("default_lib", ""),
                                  default_version=a.get("default_version", "*"),
                                  default_distributor=a.get("default_distributor", "")), "库"),
    _tool("plc_lib_install", "把一个 .library 文件安装到系统库仓库(供项目引用)。",
          _OBJ({"repository": {**_STR, "description": "目标仓库名，如 'System'"},
                "lib_path": {**_STR, "description": ".library 文件的完整路径"},
                "overwrite": {"type": "boolean", "description": "已存在时是否覆盖"}},
               ["repository", "lib_path"]),
          False, lambda a: ps_com("lib-install", repository=a["repository"],
                                  lib_path=a["lib_path"], overwrite=bool(a.get("overwrite", False)),
                                  timeout=180.0), "库", "system"),

    # ---- 写:改源码(需审批) ----
    _tool("plc_coding_profile_set", "保存当前解决方案的项目级 PLC 生成规则到 .TwinCATAgent/coding_profile.json。仅在用户明确要求改变编码规则时调用。",
          _OBJ({"author": _STR,
                "identifier_language": _STR,
                "comment_language": _STR,
                "indent_spaces": {"type": "integer", "minimum": 2, "maximum": 8},
                "require_fb_header": {"type": "boolean"},
                "require_case_state_machine": {"type": "boolean"},
                "require_transaction_timeout": {"type": "boolean"},
                "error_code_source": _STR,
                "template_first": {"type": "boolean"},
                "extra_rules": {"type": "array", "items": _STR}}),
          False, _coding_profile_set, "代码生成"),
    _tool("fblib_add", "把 fblib_find 选中的已验证模板加入当前 PLC 项目，并运行 lint。可同时添加配套 DUT 和必要库引用。",
          _OBJ({"slug": {**_STR, "description": "fblib_find 返回的模板 slug"},
                "params": {"type": "object", "additionalProperties": {"type": "string"}},
                "add_libraries": {"type": "boolean", "description": "是否添加模板依赖库，默认 true"}},
               ["slug"]),
          False, _fblib_add, "代码生成"),
    _tool("plc_create_standard_fb", "把 plc_generate 同款规则骨架一次性创建到 XAE。代码质量门禁启用时，标准事务型/循环服务型 FB 必须优先使用本工具。",
          _OBJ({"name": {**_STR, "description": "FB 名，缺少 FB_ 时自动补齐"},
                "purpose": {**_STR, "description": "一句话功能职责"},
                "author": {**_STR, "description": "可选；默认使用项目 coding profile"},
                "mode": {"type": "string", "enum": ["transaction", "service"]},
                "with_enum": {"type": "boolean", "description": "事务型是否创建配套状态枚举，默认 true"},
                "path": {**_STR, "description": "可选：目标 PLC 文件夹精确树路径"}},
               ["name", "purpose", "mode"]),
          False, _create_standard_fb, "代码生成"),
    _tool("plc_snapshot", "为当前 PLC 程序创建项目本地压缩快照。只保存规范化代码和哈希到"
          "解决方案旁的 .TwinCATAgent/snapshots，不修改 XAE 中的 PLC 对象。",
          _OBJ({"name": {**_STR, "description": "快照标签，如 before-refactor 或 last-build"}}),
          False, lambda a: plc_versions.create_snapshot(a.get("name") or "manual"), "版本"),
    _tool("plc_restore_snapshot", "预览或恢复 PLC 代码快照。默认只预览；apply=true 时先自动备份，"
          "写回代码并编译，编译失败或异常会自动回滚。对象/成员结构不一致时拒绝恢复。",
          _OBJ({"snapshot": {**_STR, "description": "快照名称或文件名，默认latest"},
                "apply": {"type": "boolean", "description": "false=预览，true=执行恢复"}}),
          False, lambda a: plc_versions.restore_snapshot(
              a.get("snapshot") or "latest", apply=bool(a.get("apply", False))), "版本"),
    _tool("plc_git_sync",
          "从 GitHub/本地 Git 仓库拉取 TwinCAT 原生 PLC 源文件，并通过 COM 写入当前已打开的 XAE。"
          "不关闭、不重新打开 XAE，也不直接覆盖 XAE 工程 XML；默认只预览。apply=true 才执行 pull、"
          "创建/更新 POU、GVL、DUT 及其 ST 方法。不会自动保存、编译、登录、下载或启动 PLC。",
          _OBJ({
              "repository": {**_STR, "description": "GitHub URL（如 https://github.com/org/repo.git）或已有本地 Git 仓库目录"},
              "local_path": {**_STR, "description": "远程仓库的本地 clone 目录；repository 为本地路径时可省略"},
              "branch": {**_STR, "description": "可选分支；已有 checkout 必须已经处于该分支"},
              "source_subdir": {**_STR, "description": "可选：仓库内 PLC 源文件子目录，禁止越出仓库"},
              "objects": {"type": "array", "items": _STR,
                          "description": "可选：只同步这些对象名；省略则同步仓库内全部 .TcPOU/.TcGVL/.TcDUT"},
              "object_paths": {"type": "object", "additionalProperties": {"type": "string"},
                               "description": "可选：对象名到当前 XAE 精确 TIPC 树路径的映射，用于重名对象"},
              "create_missing": {"type": "boolean", "description": "是否允许创建 XAE 中不存在的顶层对象/成员，默认 false"},
              "apply": {"type": "boolean", "description": "false=只预览，true=拉取并写入当前 XAE"},
          }, ["repository"]),
          False, _plc_git_sync, "改代码", "project",
          preconditions=[
              {"condition": "xae_identity", "expected": "the bound XAE host and one open solution", "check": "connect-check before repository sync"},
              {"condition": "git_repository", "expected": "a clean, verified Git checkout and parseable native PLC sources", "check": "validate repository origin, working tree, revision, source files and object mapping"},
              {"condition": "source_editor_state", "expected": "the visible PLC editor is not dirty", "check": "read the current XAE document; do not save or discard automatically", "volatile": True},
              {"condition": "code_review_gate", "expected": "each changed XAE source candidate passes the existing PLC review", "check": "review each candidate immediately before COM mutation", "volatile": True},
              {"condition": "approval", "expected": "the outer Agent permission policy allowed this project mutation", "check": "permission mode/plan is checked before dispatch"},
          ]),
    _tool("plc_write", "覆盖声明区或实现区。顶层 ST POU 需同时变更两区时，用 area=declaration、code=完整声明、implementation=完整实现；共同审核、成对写入及回读，失败尝试恢复，不是原子事务。不要用空候选预检或拆分试写。DUT 声明自动规范为 TYPE...END_TYPE，不改变 DUT 类型；写前强制审查。",
          _OBJ({"name": _STR,
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径；嵌套对象建议必传"},
                "area": {"type": "string", "enum": ["declaration", "implementation"],
                         "description": "声明区还是实现区"},
                "code": {**_STR, "description": "要写入的完整代码"},
                "implementation": {**_STR, "description": "可选：area=declaration 时同时提交顶层 ST POU 的配套实现。完整成对审核并回读，失败尝试恢复；不是 COM 原子事务。不支持成员或 DUT/GVL。"},
                "method": {**_STR, "description": "可选:写入 POU 内某个方法/动作时填其名字;留空=写 POU 本体"},
                "expected_source_hash": {**_STR, "description": "可选：候选代码基于的目标区域 SHA-256；不匹配则拒绝覆盖"},
                "expected_source_hashes": {"type": "object", "additionalProperties": {"type": "string"},
                                            "description": "可选：声明/实现区域 SHA-256 基线映射，用于并发冲突检测"}},
               ["name", "area", "code"]),
          False, _guarded_plc_write, "改代码"),
    _tool("plc_patch", "在大型 POU/方法中做一次精确的局部文本替换；要求 old_text 只出现一次。"
          "局部修改优先用本工具，避免读取并覆盖整个大文件。门禁按修改前后增量审查："
          "历史规范问题继续报告但不阻断，仅新增或恶化的问题阻断；XAE 对象树结构风险始终阻断。",
          _OBJ({"name": _STR,
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径；嵌套对象建议必传"},
                "area": {"type": "string", "enum": ["declaration", "implementation"]},
                "old_text": {**_STR, "description": "要替换的唯一原代码块"},
                "new_text": {**_STR, "description": "替换后的代码；可为空表示删除"},
                "method": {**_STR, "description": "可选成员路径，如 Start 或 Value.Get"},
                "expected_source_hash": {**_STR, "description": "可选：候选 patch 基于的目标区域 SHA-256；不匹配则拒绝覆盖"},
                "expected_source_hashes": {"type": "object", "additionalProperties": {"type": "string"},
                                            "description": "可选：声明/实现区域 SHA-256 基线映射，用于并发冲突检测"}},
               ["name", "area", "old_text", "new_text"]),
          False, _guarded_plc_patch, "改代码"),
    _tool("plc_create", "新建一个【当前不存在】的 PLC 对象。默认放在标准分类文件夹；传 path 可放入 plc_find 返回的任意嵌套 PLC 文件夹。代码质量门禁启用时，标准事务型/循环服务型 FB 禁止使用本工具，应先 fblib_find，再用 plc_create_standard_fb；门禁关闭时可自由创建，但硬结构检查仍生效。Struct/Enum/Union/Alias 可传完整 TYPE...END_TYPE 或编辑器中的简写正文；工具会规范化并核验 XAE 实际 itemType。"
          "⚠️禁止用于已存在的对象——给已有 FB/程序/接口添加方法(method)/属性/动作，"
          "必须改用 plc_create_member，绝不要用本工具重建已存在的对象。",
          _OBJ({"name": _STR,
                "type": {"type": "string",
                         "enum": ["fb", "program", "function", "struct", "enum", "union", "alias", "gvl", "interface"],
                         "description": "对象类型；function 必须提供 return_type。"},
                "path": {**_STR, "description": "可选：目标 PLC 文件夹的精确树路径；不传=标准分类文件夹"},
                "return_type": {**_STR, "description": "function 的返回类型，例如 INT。"},
                "declaration": _STR, "implementation": _STR}, ["name", "type"]),
          False, _guarded_plc_create, "改代码"),
    _tool("plc_create_folder", "在当前 PLC 树的指定父节点下新建文件夹。可在标准 POUs 文件夹或已有嵌套文件夹/支持文件夹的节点下继续创建对象；必须传 plc_find/plc_structure 返回的精确 parent_path，避免写入错误项目。",
          _OBJ({"name": {**_STR, "description": "新文件夹名称，不能包含 ^"},
                "parent_path": {**_STR, "description": "父节点精确树路径；省略时默认当前 PLC 的 POUs 文件夹"}},
               ["name"]),
          False, lambda a: ps_com("new-folder", name=a["name"],
                                  parent_path=a.get("parent_path") or ""), "改代码"),
    _tool("plc_create_member",
          "在已有 POU/接口内部创建方法(method)、动作(action)或转换(transition)。Property 必须改用 plc_create_property，禁止只创建属性壳。",
          _OBJ({"pou": {**_STR, "description": "所属 POU 名，如 FB_Motor"},
                "path": {**_STR, "description": "可选：plc_find 返回的所属 POU 精确树路径"},
                "name": {**_STR, "description": "成员名，如 Start"},
                "member_type": {"type": "string", "enum": ["method", "action", "transition", "propget", "propset"]},
                "return_type": {**_STR, "description": "方法/属性的返回类型，如 BOOL；无返回或动作可留空"},
                "declaration": _STR, "implementation": _STR},
               ["pou", "name", "member_type"]),
          False, _guarded_plc_create_member, "改代码"),
    _tool("plc_create_property",
          "在已有 FB/程序中一次性创建 Property 及其 Get/可选 Set 访问器，并写入实现。会复用 TwinCAT 自动创建的访问器或补建缺失子节点；失败时回滚属性壳。接口 Property 因 XAE COM 崩溃风险暂不支持。",
          _OBJ({"pou": {**_STR, "description": "所属 FB/程序名"},
                "path": {**_STR, "description": "可选：所属 POU 精确树路径"},
                "name": {**_STR, "description": "属性名，如 State"},
                "return_type": {**_STR, "description": "属性类型，如 E_State"},
                "getter_implementation": {**_STR, "description": "Get 实现体，必填"},
                "setter_implementation": {**_STR, "description": "Set 实现体；只读属性留空"}},
               ["pou", "name", "return_type", "getter_implementation"]),
          False, _create_property, "改代码"),
    _tool("plc_delete", "删除一个 POU/DUT/GVL/接口/VISU。默认先检查引用；dry_run=true 只预览，"
          "有引用时须显式 force=true 才删除。",
          _OBJ({"name": _STR,
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径"},
                "dry_run": {"type": "boolean", "description": "只预览引用和阻断结果"},
                "force": {"type": "boolean", "description": "有引用时仍强制删除"}}, ["name"]),
          False, lambda a: ps_com("delete-pou", name=a["name"],
                                  path=a.get("path") or "",
                                  dry_run=bool(a.get("dry_run", False)),
                                  force=bool(a.get("force", False))), "改代码"),
    _tool("plc_save_document", "用户明确要求保存时，按次审批保存一个已打开的PLC父文档。先plc_read(document_baseline=true,name,path)获取基线。核对实时代码/成员/磁盘版本后调用Document.Save；不SaveAll、不关闭工程、不编译、不上线。未保存本身不是拒绝条件；基线过期必须重读并重新审批。",
          _OBJ({"name": _STR, "path": _STR,
                "expected_document_baseline": {"type": "object", "description": "原样传入plc_read返回的document_baseline，禁止自行构造"}},
               ["name", "path", "expected_document_baseline"]),
          False, lambda a: ps_com('save-document', **a), '改代码', 'save_document'),
    _tool("plc_delete_member", "删除 POU/接口内部的 Method、Property、Action、Transition 或属性访问器。",
          _OBJ({"pou": {**_STR, "description": "所属 POU/接口名，如 FB_Motor"},
                "name": {**_STR, "description": "成员名；删除访问器时填写属性名"},
                "member_type": {"type": "string", "enum": ["method", "property", "action", "transition", "propget", "propset"]},
                "expected_member_baseline": _MEMBER_BASELINE_SCHEMA,
                "path": {**_STR, "description": "可选：plc_find 返回的所属 POU 精确树路径"},
                "dry_run": {"type": "boolean", "description": "只预览引用和阻断结果"},
                "force": {"type": "boolean", "description": "有引用时仍强制删除"}},
               ["pou", "name", "member_type"]),
          False, lambda a: ps_com("delete-member", pou=a["pou"], name=a["name"],
                                  **({'expected_member_baseline': a['expected_member_baseline']} if 'expected_member_baseline' in a else {}),
                                  type=a["member_type"], path=a.get("path") or "",
                                  dry_run=bool(a.get("dry_run", False)),
                                  force=bool(a.get("force", False))), "改代码"),
    _tool("plc_rename", "重命名一个 POU/DUT/GVL/接口。",
          _OBJ({"old": _STR, "new": _STR,
                "path": {**_STR, "description": "可选：plc_find 返回的精确树路径"}},
               ["old", "new"]),
          False, lambda a: ps_com("rename", old=a["old"], new=a["new"],
                                  path=a.get("path") or ""), "改代码"),
    _tool("plc_rename_member", "重命名 POU/接口内部的 Method、Property、Action 或 Transition。",
          _OBJ({"pou": _STR, "old": _STR, "new": _STR,
                "expected_member_baseline": _MEMBER_BASELINE_SCHEMA,
                "path": {**_STR, "description": "可选：所属 POU 的精确树路径"}},
               ["pou", "old", "new"]),
          False, lambda a: ps_com("rename-member", pou=a["pou"], old=a["old"],
                                  **({'expected_member_baseline': a['expected_member_baseline']} if 'expected_member_baseline' in a else {}),
                                  new=a["new"], path=a.get("path") or ""), "改代码"),

    # ---- 写:运行时(破坏性,需审批) ----
    _tool("tc_activate", "激活配置(写注册表;需紧跟 restart 才真正加载；不切换 Config 模式)。",
          _OBJ(), False, lambda a: ps_com("activate"), "运行时", "runtime"),
    _tool("tc_restart", "重启 TwinCAT 运行时。",
          _OBJ(), False, lambda a: ps_com("restart", timeout=60.0), "运行时", "runtime"),
    _tool("tc_login", "登录选定 PLC；多 PLC 必须指定 runtime 或 all_plcs。接受请求不证明登录/下载完成。", _OBJ({'runtime': _STR, 'all_plcs': {'type': 'boolean'}}), False,
          lambda a: _runtime_command("login", args=a), "运行时", "runtime"),
    _tool("tc_logout", "登出选定 PLC 的 IDE 会话；不代表 PLC 停止。", _OBJ({'runtime': _STR, 'all_plcs': {'type': 'boolean'}}), False,
          lambda a: _runtime_command("logout", args=a), "运行时", "runtime"),
    _tool("tc_start", "仅用户要求启动且尚未 Run 时调用。login 已返回 Run 时不要再调用；already_satisfied 表示已运行、未发送 Start，不得称已执行启动。多 PLC 必须明确选择。", _OBJ({'runtime': _STR, 'all_plcs': {'type': 'boolean'}}), False,
          lambda a: _runtime_command("start", {5}, args=a), "运行时", "runtime"),
    _tool("tc_stop", "停止选定 PLC，按实际 ADS 端口验证 Stop。", _OBJ({'runtime': _STR, 'all_plcs': {'type': 'boolean'}}), False,
          lambda a: _runtime_command("stop", {6}, args=a), "运行时", "runtime"),
    _tool("tc_online", "确认选定 PLC 登录且 ProgramLoaded 后启动，逐实际 ADS 端口验证；证据不足即停止，不自动确认下载弹窗，不证明程序版本一致。", _OBJ({'runtime': _STR, 'all_plcs': {'type': 'boolean'}}), False,
          lambda a: _runtime_command("online", {5}, 30.0, args=a), "运行时", "runtime"),
    _tool("plc_read_values",
          "通过当前目标的 ADS 符号表批量读取在线 PLC 标量变量。可给每项提供 expected 和 tolerance，"
          "形成运行时断言；多 PLC 项目必须用 runtime 或 ads_port 指定实例。只接受符号名，不接受原始地址。",
          _OBJ({
              "runtime": {**_STR, "description": "可选 PLC runtime 名称；仅一个 runtime 时可省略"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "symbols": {"type": "array", "minItems": 1, "maxItems": 32,
                          "items": {"type": "object", "properties": {
                              "name": _STR,
                              "type": {"type": "string", "enum": sorted(_PLC_VALUE_TYPES)},
                              "expected": {},
                              "tolerance": {"type": "number", "minimum": 0},
                          }, "required": ["name", "type"], "additionalProperties": False}},
          }, ["symbols"]), True, _plc_read_values, "运行时"),
    _tool("plc_write_values",
          "通过 ADS 符号名批量写入在线 PLC 标量变量；先读取旧值，可用 expected_before 做比较后写入，"
          "写入后立即回读并报告 matched。仅允许当前项目已发现的 Run 状态 PLC runtime；非原子批量写入。",
          _OBJ({
              "runtime": {**_STR, "description": "可选 PLC runtime 名称；仅一个 runtime 时可省略"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "values": {"type": "array", "minItems": 1, "maxItems": 32,
                         "items": {"type": "object", "properties": {
                             "name": _PLC_SYMBOL_SCHEMA,
                             "type": {"type": "string", "enum": sorted(_PLC_VALUE_TYPES)},
                             "value": {}, "expected_before": {},
                             "tolerance": {"type": "number", "minimum": 0},
                         }, "required": ["name", "type", "value"], "additionalProperties": False}},
          }, ["values"]), False, _plc_write_values, "运行时", "runtime"),
    _tool("plc_read_value",
          "自动从 TwinCAT 在线符号表识别类型并即时读取一个变量，无需提供类型。支持标量、字符串、"
          "枚举、数组元素和结构体成员路径；读取整个结构体或数组时返回最多 256 个叶子值及类型元数据。",
          _OBJ({
              "runtime": {**_STR, "description": "可选 PLC runtime 名称；仅一个 runtime 时可省略"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "name": _PLC_SYMBOL_SCHEMA,
              "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
              "expected": {},
              "tolerance": {"type": "number", "minimum": 0},
          }, ["name"]), True, _plc_read_value, "运行时"),
    _tool("plc_write_value",
          "自动识别在线 PLC 变量类型并即时写入一个叶子变量，支持标量、字符串、枚举、数组元素和"
          "结构体成员。写前读取旧值，可用 expected_before 门禁，写后立即回读；禁止整结构体/数组、"
          "指针、引用和只读符号写入。",
          _OBJ({
              "runtime": {**_STR, "description": "可选 PLC runtime 名称；仅一个 runtime 时可省略"},
              "ads_port": {"type": "integer", "minimum": 1, "maximum": 65535},
              "name": _PLC_SYMBOL_SCHEMA, "value": {}, "expected_before": {},
              "tolerance": {"type": "number", "minimum": 0},
          }, ["name", "value"]), False, _plc_write_value, "运行时", "runtime"),
    _tool("tc_config_mode",
          "把当前目标切到 Config 模式，并通过 ADS System Service(Port 10000) 验证结果。"
          "优先通过 ADS System Service(Port 10000) 请求 RECONFIG，再回退到 XAE；"
          "不会激活配置。",
          _OBJ(), False, _set_config_mode, "运行时", "runtime"),
    _tool("tc_run_mode", "把当前目标切到 Run 模式，并通过 ADS System Service Port 10000 验证结果。",
          _OBJ(), False, _set_run_mode, "运行时", "runtime"),
]

REGISTRY += [
    _tool("nc_structure", "Read the configured NC task and axis tree.",
          _OBJ({"max_depth": {"type": "integer"}}), True,
          lambda a: ps_com("nc-structure", max_depth=int(a.get("max_depth") or 6)), "NC"),
    _tool("nc_axis_info", "Read NC axis type, path, drive, encoder and parameters.",
          _OBJ({"axis": _STR}, ["axis"]), True,
          lambda a: ps_com("nc-axis-info", axis=a["axis"]), "NC"),
    _tool("nc_axis_params", "Read NC axis scaling, dynamics and monitoring parameters.",
          _OBJ({"axis": _STR}, ["axis"]), True,
          lambda a: ps_com("nc-axis-params", axis=a["axis"]), "NC"),
    _tool("nc_axis_params_set", "Set allow-listed NC axis parameters and verify by readback.",
          _OBJ({"axis": _STR, "parameters": {"type": "object"}}, ["axis", "parameters"]),
          False, lambda a: ps_com("nc-axis-params-set", axis=a["axis"],
                                 parameters=a["parameters"]), "NC", "project"),
    _tool("nc_drive_list", "Find EtherCAT servo-drive candidates in the I/O tree.",
          _OBJ(), True, lambda a: ps_com("nc-drive-list"), "NC"),
    _tool("nc_encoder_list", "Find encoder and position-feedback candidates in the I/O tree.",
          _OBJ(), True, lambda a: ps_com("nc-encoder-list"), "NC"),
    _tool("nc_links", "Read NC axis drive and encoder link summaries.",
          _OBJ(), True, lambda a: ps_com("nc-links"), "NC"),
    _tool("nc_state", "Read runtime and configured NC state without inventing online axis data.",
          _OBJ(), True, lambda a: ps_com("nc-state"), "NC"),
    _tool("nc_axis_state", "Read verifiable state for one NC axis.",
          _OBJ({"axis": _STR}, ["axis"]), True,
          lambda a: ps_com("nc-axis-state", axis=a["axis"]), "NC"),
    _tool("nc_axis_move", "Run a monitored absolute move on a virtual or physical NC axis.",
          _OBJ({"axis": _STR, "position": {"type": "number"},
                "velocity": {"type": "number"}, "timeout": {"type": "number"},
                "control_scope": _STR, "confirm_physical": {"type": "boolean"},
                "require_homed": {"type": "boolean"}},
               ["axis", "position", "velocity"]), False,
          lambda a: ps_com(
              "nc-axis-move", timeout=max(45.0, float(a.get("timeout", 30.0)) + 15.0),
              axis=a["axis"], position=a["position"], velocity=a["velocity"],
              move_timeout=a.get("timeout", 30.0), control_scope=a.get("control_scope", ""),
              confirm_physical=a.get("confirm_physical", False),
              require_homed=a.get("require_homed", True)), "NC", "runtime"),
    _tool("nc_validate", "Compare an NC task/axis manifest with the current configuration.",
          _OBJ({"configuration": {"type": "object"}}, ["configuration"]), True,
          lambda a: ps_com("nc-validate", configuration=a["configuration"]), "NC"),
    _tool("nc_create_task", "Create an NC task under TINC.",
          _OBJ({"name": _STR}), False,
          lambda a: ps_com("nc-create-task", name=a.get("name") or "NC-Task"),
          "NC", "project"),
    _tool("nc_create_axis", "Create a continuous or virtual NC axis.",
          _OBJ({"task": _STR, "name": _STR,
                "axis_type": {"type": "string", "enum": ["continuous", "virtual"]}},
               ["task", "name"]), False,
          lambda a: ps_com("nc-create-axis", task=a["task"], name=a["name"],
                           axis_type=a.get("axis_type") or "continuous"),
          "NC", "project"),
    _tool("nc_link_drive", "Link an NC axis to a CoE or SoE drive using the Automation Interface equivalent of NC Settings > Link To; TwinCAT generates all PDO mappings.",
          _OBJ({"axis": _STR, "drive_path": _STR, "channel": {"type": "integer"}},
               ["axis", "drive_path"]), False,
          lambda a: ps_com("nc-link-drive", axis=a["axis"],
                           drive_path=a["drive_path"], channel=a.get("channel")),
          "NC", "project"),
    _tool("nc_quick_link", "Quickly link the unique unlinked NC axis to the unique detected servo drive. No paths are required for the common one-axis/one-drive case.",
          _OBJ({"axis": _STR, "drive_path": _STR}), False,
          lambda a: ps_com("nc-quick-link", axis=a.get("axis") or "",
                           drive_path=a.get("drive_path") or ""),
          "NC", "project"),
    _tool("nc_link_encoder", "Link an NC axis to an explicit encoder path.",
          _OBJ({"axis": _STR, "encoder_path": _STR}, ["axis", "encoder_path"]), False,
          lambda a: ps_com("nc-link-encoder", axis=a["axis"],
                           encoder_path=a["encoder_path"]), "NC", "project"),
]

REGISTRY.append(
    _tool("plc_static_constraints",
          "读取 PLC 写前静态约束及 Agent 自有检查覆盖。无需 TE1200 授权；SA 编号仅作规则参考，不代表官方检查通过。",
          _OBJ({"rule_id": {**_STR, "description": "可选 SA 编号；省略返回全部 Agent 基线"}}),
          True, lambda a: plc_static_constraints(a.get("rule_id") or ""), "代码")
)

REGISTRY.append(
    _tool("plc_static_analysis",
          "Run offline TwinCAT Agent Static Analysis (TCSA) on all PLC source. "
          "This is read-only and does not require TE1200, but it cannot prove a TE1200 pass.",
          _OBJ({"max_complexity": {"type": "integer", "minimum": 1,
                                    "description": "ST subset cognitive-complexity warning threshold; default 20"},
                "rule_severities": _STATIC_RULE_SEVERITIES}),
          True, lambda a: analyze_objects(
              ps_com("all-code"), max_complexity=max(1, int(a.get("max_complexity") or 20)),
              rule_severities=a.get("rule_severities")), "代码")
)


# Compile the single contract view once. Tools without specialized conditions
# still carry an explicit argument/handler contract, not implicit XAE access.
for _registered_tool in REGISTRY:
    from tc_agent.plc_path_contract import extend_schema
    extend_schema(_registered_tool)
    if not _registered_tool.get("preconditions"):
        _registered_tool["preconditions"] = contract_for_tool(
            _registered_tool["name"],
            str(_registered_tool.get("category") or ""),
            bool(_registered_tool.get("readonly")),
        )
    _registered_tool["contract"] = compile_contract(_registered_tool)
    _registered_tool["preconditions"] = _registered_tool["contract"]["preconditions"]


_BY_NAME = {t["name"]: t for t in REGISTRY}


def tools_schema(allowed_categories: set[str] | None = None,
                 allowed_names: set[str] | None = None) -> list[dict]:
    """给模型看的中立 schema(不含执行器)。"""
    from tc_agent.tool_recovery import recovery_dependencies
    selected = {t['name'] for t in REGISTRY
                if (allowed_categories is None or t.get('category') in allowed_categories)
                and (allowed_names is None or t['name'] in allowed_names)}
    dependencies = recovery_dependencies(selected)
    from tc_agent.tool_surface import model_selection
    selected = model_selection(selected)
    result = []
    for t in REGISTRY:
        if t['name'] not in selected | dependencies:
            continue
        description = str(t["description"]) + schema_contract_note(t["contract"])
        result.append({"name": t["name"], "description": description,
                       "parameters": t["parameters"]})
    return result


def is_readonly(name: str) -> bool:
    t = _BY_NAME.get(name)
    return bool(t and t["readonly"])


def tool_category(name: str) -> str:
    tool = _BY_NAME.get(name)
    return str(tool.get("category") or "") if tool else ""


def tool_danger(name: str) -> str:
    tool = _BY_NAME.get(name)
    return str(tool.get("danger") or "") if tool else ""


def tool_metadata(name: str) -> dict:
    """Return the execution contract without exposing the callable itself."""
    tool = _BY_NAME.get(name)
    if not tool:
        return {}
    return {
        "name": tool["name"],
        "category": str(tool.get("category") or ""),
        "readonly": bool(tool.get("readonly")),
        "danger": str(tool.get("danger") or ""),
        "protocol_version": int(tool.get("protocol_version") or 1),
        "side_effect": str(tool.get("side_effect") or ""),
        "idempotency": str(tool.get("idempotency") or ""),
        "preconditions": list(tool.get("preconditions") or []),
        "contract": json.loads(json.dumps(tool["contract"], ensure_ascii=False)),
    }


def validate_tool_arguments(name: str, args: dict) -> dict | None:
    """Read-only contract validation; does not resolve endpoints or approve tools."""
    from tc_agent.tool_arguments import argument_failure, validate_arguments
    tool = _BY_NAME.get(name)
    if tool is None:
        return None
    schema = tool['parameters']
    if not isinstance(args, dict):
        return argument_failure(name, schema, [{'field': '$', 'rule': 'type', 'expected': 'object'}])
    try:
        normalized = _normalize_tool_arguments(name, args, schema)
    except (ValueError, TypeError):
        return argument_failure(name, schema, [{'field': '$', 'rule': 'normalization'}])
    invalid = validate_arguments(name, normalized, schema)
    if invalid:
        return invalid
    try:
        from tc_agent.plc_path_contract import normalize
        normalize(name, normalized)
    except ValueError as exc:
        failure = argument_failure(name, schema, [{'field': 'tree_path/source_path', 'rule': 'path_identity'}])
        failure['error'] = str(exc)
        failure['next_action'] = 'tree_path 使用 plc_find 返回的精确 COM 路径；source_path 使用 plc_source_catalog 返回的索引身份，不相互转换。'
        return failure
    return None


def run_tool(name: str, args: dict, prefer_pid: int = 0) -> object:
    from tc_agent.tool_error_policy import annotate_error
    from tc_agent.execution_policy import tool_succeeded
    result = _run_tool_impl(name, args, prefer_pid)
    # Preserve successful direct-call payloads for existing CLI/API consumers.
    # Foreground/worker boundaries still annotate successes and warnings.
    return result if tool_succeeded(result) else annotate_error(result, False)


def _run_tool_impl(name: str, args: dict, prefer_pid: int = 0) -> object:
    t = _BY_NAME.get(name)
    if t is None:
        return {"status": "unsupported", "error_type": "unknown_tool",
                "error": f"unknown tool: {name}", "not_executed": True,
                "condition": "tool_registry", "expected": "registered tool",
                "actual": {"name": name}, "scope": {},
                "reason": "工具未在当前 Agent 注册表中。",
                "next_action": "使用当前 tools_schema 选择已注册工具；不要改用未声明的入口。"}
    if not isinstance(t.get("contract"), dict) or not t["contract"].get("preconditions"):
        return precondition_failure(status="blocked", condition="tool_contract",
            expected="registered versioned tool contract", actual={}, scope={"tool": name},
            reason="工具缺少统一契约，拒绝执行。", next_action="修复工具注册和契约后重试，不绕过执行入口。")
    invalid = validate_tool_arguments(name, args)
    if invalid is not None:
        return invalid
    try:
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        args = _normalize_tool_arguments(name, args, t["parameters"])
        from tc_agent.plc_path_contract import normalize as normalize_plc_paths
        args = normalize_plc_paths(name, args)
        with tool_target(prefer_pid):
            if (name == "tc_hmi_ads_symbol_set" and args.get("apply")
                    and args.get("action", "upsert") != "remove"):
                return {"status": "blocked", "written": False,
                        "reason": "Agent 不应用未经符号解析核实的静态 ADS 数字地址。请用 tc_hmi_bind_plc；找不到 TMC 根时先检查 PLC 调用链和编译导出。",
                        "recommended_tools": ["tc_hmi_bind_plc", "plc_structure", "plc_build"]}
            precondition = _tool_precondition_failure(name, args, prefer_pid, t)
            if precondition is not None:
                return precondition
            if (name.startswith("tc_hmi_") and "file" in args
                    and not (name == "tc_hmi_source_catalog" and args["file"] == "")):
                validate_hmi_file(args["file"], args.get("project") or None)
            result = t["run"](args)
            if name == "plc_read" and isinstance(result, dict) and not result.get("error"):
                # Only complete live text can be used as a write baseline.
                result = {**result, "source_hashes": {
                    area: hashlib.sha256(result[area].encode("utf-8")).hexdigest()
                    for area in ("declaration", "implementation")
                    if isinstance(result.get(area), str)
                    and not result.get(f"{area}_paging", {}).get("truncated")
                    and int(result.get(f"{area}_paging", {}).get("start_line", args.get("start_line") or 1)) == 1
                    and not result.get("truncated")
                }}
            return result
    except TcComError as e:
        if is_source_mutation_tool(name):
            login_error = editor_login_failure(e)
            if login_error:
                return login_error
        return {"error": f"COM 操作失败: {e}", "error_type": "TcComError"}
    except ValueError as e:
        return {"error": f"参数无效: {e}", "error_type": "ValueError", **getattr(e, "details", {})}
    except KeyError as e:
        return {"error": f"缺少参数: {e}", "error_type": "KeyError"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"工具执行异常: {e}", "error_type": type(e).__name__}


# ======================================================================
#  HTTP 小工具
# ======================================================================
def _opener(proxy: str | None):
    """按需构造 opener,显式控制代理,不继承系统/环境里的代理设置。

    这是根治 WinError 10061「目标计算机积极拒绝」的关键:Clash 等工具开启时会把
    Windows 系统代理设成 127.0.0.1:7890,urllib 默认 opener 会继承它;一旦把代理
    软件关掉,系统代理项常常还在(僵尸),urllib 仍去连那个已经没人监听的本地端口
    → 直接被拒。产品自带的 Provider(如国内可直连的 DeepSeek)默认【不走任何代理】,
    只有用户在该 Provider 里显式填了代理(如从国内访问 Anthropic 官方)才走。
      proxy 为空/None → ProxyHandler({}) 空字典 = 禁用一切代理(含继承来的系统代理)
      proxy 为 URL    → 走这个代理"""
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    return urllib.request.build_opener(handler)


def _post_json(url: str, headers: dict, body: dict, proxy: str | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with _opener(proxy).open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"API {e.code}: {detail[:600]}") from e


def _post_sse(url: str, headers: dict, body: dict, proxy: str | None = None):
    """POST 并按 SSE 逐帧产出 (event_name|None, json_obj)。OpenAI 只有 data,
    Anthropic 有 event+data,两者统一处理。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        **headers, "Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        # 60s 是"两次数据之间的最大间隔"(socket 读超时)。设太大(如 300s)时,
        # 模型流式偶发卡住会让整轮假死好几分钟;60s 内没新数据就抛错,由上层重试/报错。
        resp = _opener(proxy).open(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {e.code}: {e.read().decode('utf-8', 'replace')[:600]}") from e
    try:
        event = None
        for raw in resp:                               # 按行迭代,不会切断多字节字符
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                event = None
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                yield event, parsed
    finally:
        resp.close()


# ======================================================================
#  Provider 适配层
# ======================================================================
TOOL_RESULT_CAP = 12000  # 普通工具结果；较早正文由请求级上下文分区投影精简
DIAGNOSTIC_RESULT_CAP = 32000
# plc_read already supports area/start_line/max_lines pagination.  Source code
# needs a larger dedicated budget than inventories/build diagnostics: applying
# the generic 3500-character head/tail cap hid the middle of declarations and
# implementations and made the model repeatedly read the same POU.
PLC_READ_RESULT_CAP = 26000
# Batch reads use one COM round-trip, but a 100K source dump would immediately
# hit the generic context cap and become a misleading head/tail splice.
PLC_BATCH_MAX_TOTAL_CHARS = 20000


_RESULT_PRIORITY_KEYS = (
    # Outcome and acceptance evidence must survive every fallback.  Identity
    # fields come afterwards so a large project path cannot crowd out a failed
    # phase, incomplete diagnostics or the recovery instruction.
    "ok", "success", "status", "error", "exception", "message", "reason",
    "phase", "stage", "verified", "valid", "build_succeeded",
    "diagnostics_available", "diagnostics_complete", "diagnostics_pending",
    "compiler_verified", "verification_scope", "repair_allowed", "diagnostic_fingerprint",
    "error_policy", "page_verified", "runtime_bindings_verified", "next_action",
    "recovery_exhausted", "diagnostic_recovery", "capability_exhausted", "semantic_evidence", "unsupported_reasons",
    "member_baseline",
    "error_count", "warning_count", "failed_projects", "truncated",
    "name", "project", "file", "path", "full_path", "count", "total", "readonly",
)


def _result_item_priority(value) -> int:
    """Rank list entries by diagnostic value without reclassifying success."""
    if not isinstance(value, dict):
        return 0
    score = 0
    severity = str(value.get("severity") or value.get("level") or "").lower()
    status = str(value.get("status") or "").lower()
    if severity in {"fatal", "error"} or status in {"failed", "error", "incomplete"}:
        score += 100
    elif severity in {"warning", "warn"} or status in {"warning", "partial-failure"}:
        score += 50
    if any(key in value for key in ("error", "exception", "reason", "finding", "rule")):
        score += 20
    if any(value.get(key) is False for key in ("ok", "success", "verified", "valid")):
        score += 80
    return score


def _compact_json_value(value, depth: int = 0):
    """Create a small, valid JSON-shaped preview without splicing serialization."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cap = 480 if depth < 2 else 240
        if len(value) <= cap:
            return value
        return value[:cap] + f"…<{len(value) - cap} chars omitted>"
    if isinstance(value, list):
        # Keep the first item for orientation, then prefer concrete failures and
        # warnings wherever they occur.  A plain head slice frequently hid the
        # only build error at the end of a long diagnostic collection.
        selected = list(range(min(1, len(value))))
        ranked = sorted(range(len(value)), key=lambda i: (-_result_item_priority(value[i]), i))
        for index in ranked:
            if index not in selected:
                selected.append(index)
            if len(selected) >= 4:
                break
        selected.sort()
        kept = [_compact_json_value(value[index], depth + 1) for index in selected]
        if len(value) > len(selected):
            kept.append({"_omitted_items": len(value) - len(selected),
                         "_total_items": len(value)})
        return kept
    if isinstance(value, dict):
        keys = list(value)
        ordered = [key for key in _RESULT_PRIORITY_KEYS if key in value]
        ordered.extend(key for key in keys if key not in ordered)
        kept = {}
        for key in ordered[:16]:
            kept[str(key)] = _compact_json_value(value[key], depth + 1)
        if len(keys) > len(kept):
            kept["_omitted_keys"] = len(keys) - len(kept)
        return kept
    return _compact_json_value(str(value), depth)


def cap_tool_result(result, limit: int = TOOL_RESULT_CAP):
    """Bound model context while preserving valid, queryable JSON structure.

    Full output remains available to the UI and durable execution ledger.  The
    previous implementation joined the beginning and end of serialized JSON in
    one string, which hid field boundaries and encouraged repeated broad reads.
    """
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return result
    envelope = {
        "_capped": True,
        "original_chars": len(raw),
        "message": "结果过大；以下为结构化摘要。请使用筛选或分页参数缩小读取范围。",
        "summary": _compact_json_value(result),
    }
    while len(json.dumps(envelope, ensure_ascii=False)) > limit:
        summary = envelope.get("summary")
        if isinstance(summary, dict) and len(summary) > 1:
            removable = [
                key for key in summary
                if key not in _RESULT_PRIORITY_KEYS and not str(key).startswith("_")
            ]
            if removable:
                summary.pop(removable[-1], None)
                summary["_omitted_keys"] = summary.get("_omitted_keys", 0) + 1
                continue
        envelope["summary"] = {
            "type": type(result).__name__,
            "top_level_keys": list(result)[:20] if isinstance(result, dict) else [],
        }
        break
    if not tool_succeeded(result):
        # Preserve failures even in the metadata-only fallback.
        envelope["ok"] = False
        if isinstance(result, dict):
            for key in ("status", "phase", "retry_safe", "files_preserved", "verified", "isError"):
                if key in result:
                    envelope[key] = _compact_json_value(result[key])
            if "error" in result:
                envelope["error"] = str(result["error"])[:1000]
    from tc_agent.result_decision import decision
    envelope['decision_evidence'] = decision(result)
    envelope['message'] = '结果正文已精简；以 decision_evidence 为判据。省略字段不代表通过；不要通过重复执行或缩小写入候选获取判据。'
    return envelope


def _hmi_read_result_for_context(args: dict, result, limit: int = 6000):
    """Deliver bounded contiguous pages with cursors for what the MODEL saw."""
    if not isinstance(result, dict):
        return cap_tool_result(result, limit)
    if not tool_succeeded(result):
        return cap_tool_result(result, limit)
    if result.get('status') != 'read':
        return cap_tool_result(result, limit)
    control_id = str(args.get("control_id") or result.get("control_id") or "")
    controls = list(result.get("controls") or [])
    bindings = list(result.get("bindings") or [])
    if control_id:
        controls = [item for item in controls if str(item.get("id") or "") == control_id]
        bindings = [item for item in bindings if str(item.get("control") or "") == control_id]
    else:
        controls = [
            {"id": item.get("id"), "type": item.get("type")}
            for item in controls if isinstance(item, dict)
        ]
    summary = {
        key: result.get(key) for key in (
            "status", "project", "file", "full_path", "control_id",
            "content_included", "content_chars", "truncated", "control_count",
            "total_control_count", "returned_control_count", "controls_truncated",
            "binding_count", "returned_binding_count", "bindings_truncated", "readonly",
            "source", "live_xae", "dirty_unknown", "source_consistent", "source_size", "source_mtime_ticks",
            "control_offset", "content_offset", "total_content_chars",
            "area", "read_layer", "cache_hit", "source_hash", "parse_error", "elapsed_ms",
            "authoritative", "bindings_scope",
        ) if key in result
    }
    summary['controls'] = []
    summary['bindings'] = []
    source_requested = not control_id or args.get('include_content') is True or result.get('source_requested') is True
    content = str(result.get('content') or result.get('content_preview') or '') if source_requested else ''
    summary['source_requested'] = source_requested and bool(result.get('content_included', bool(content)))
    def size():
        return len(json.dumps(summary, ensure_ascii=False))
    # Reserve room for explicit truncation metadata/cursors and requested source.
    controls_budget = int(limit * .55) if content else limit - 1300
    for item in controls:
        summary['controls'].append(item)
        if size() > controls_budget:
            summary['controls'].pop()
            break
    for item in bindings:
        summary['bindings'].append(item)
        if size() > (int(limit * .65) if content else limit - 1000):
            summary['bindings'].pop()
            break
    summary['returned_control_count'] = len(summary['controls'])
    summary['returned_binding_count'] = len(summary['bindings'])
    summary['controls_truncated'] = bool(result.get('controls_truncated') or len(controls) > len(summary['controls']))
    summary['bindings_truncated'] = bool(result.get('bindings_truncated') or len(bindings) > len(summary['bindings']))
    offset = int(result.get('control_offset') or args.get('control_offset') or 0)
    summary['next_control_offset'] = offset + len(summary['controls']) if summary['controls_truncated'] and not control_id else None
    summary['next_content_offset'] = None
    if summary['controls_truncated'] or summary['bindings_truncated']:
        summary['narrowing_hint'] = '控件目录按 next_control_offset 续读；完整属性/绑定用 control_id 精读。'
    if controls and not summary['controls'] and not control_id:
        summary['next_control_offset'] = None
        summary['message'] = '单个目录项超出上下文预算，请切换源码分页，不要重复当前控件页。'
    if control_id and len(summary['controls']) < len(controls):
        summary['message'] = '单个控件属性过大，未拼接或截断属性；请用 include_content=true 且不指定 control_id 分页读取源码。'
    elif control_id and not controls:
        summary['message'] = f'结果中没有控件 {control_id!r}；请检查精确 ID。'
    if content:
        start = int(result.get('content_offset') or args.get('content_offset') or 0)
        # Size a contiguous prefix by serialized length, never splice head/tail.
        low, high = 0, len(content)
        while low < high:
            mid = (low + high + 1) // 2
            summary['content'] = content[:mid]
            if size() <= limit - 400:
                low = mid
            else:
                high = mid - 1
        summary['content'] = content[:low]
        delivered = len(summary['content'].encode('utf-16-le')) // 2
        summary['content_chars'] = delivered
        summary['content_offset'] = start
        summary['truncated'] = bool(result.get('truncated') or result.get('content_preview_truncated') or low < len(content))
        summary['next_content_offset'] = start + delivered if summary['truncated'] else None
    elif control_id:
        summary['content_included'] = False
        summary['content_chars'] = 0
    return summary


def _hmi_index_result_for_context(args, result, limit=6000):
    """Keep indexed records atomic and cursors aligned with delivered records."""
    if not isinstance(result, dict) or not tool_succeeded(result):
        return cap_tool_result(result, limit)
    if result.get('area') == 'source':
        return _hmi_read_result_for_context(args, result, limit)
    if result.get('content_included'):
        # Mixed detail/source requests still retain atomic events and attributes.
        details = {k: v for k, v in result.items() if k != 'content'}
        details.update(content_included=False, content_chars=0, next_content_offset=None)
        out = _hmi_index_result_for_context(args, details, max(2000, int(limit * .6)))
        text = str(result.get('content') or '')
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            out['content'] = text[:mid]
            if len(json.dumps(out, ensure_ascii=False)) <= limit - 400:
                low = mid
            else:
                high = mid - 1
        out['content'] = text[:low]
        out['content_included'] = True
        out['content_chars'] = len(out['content'].encode('utf-16-le')) // 2
        out['truncated'] = bool(result.get('truncated') or low < len(text))
        out['next_content_offset'] = (int(result.get('content_offset', 0)) + out['content_chars']
                                      if out['truncated'] else None)
        return out
    if result.get('status') not in {'read', 'catalog'}:
        return cap_tool_result(result, limit)
    out = dict(result)
    is_catalog = result.get('status') == 'catalog'
    key = 'items' if is_catalog else 'controls'
    items = list(result.get(key) or [])
    out[key] = list(items)
    # Index warnings have their own totals; do not let them consume a whole page.
    if is_catalog and 'index_sync' in out:
        info = dict(out['index_sync'])
        info['issues'] = list(info.get('issues') or [])[:3]
        info['issues_truncated'] = info.get('issue_count', 0) > len(info['issues'])
        out['index_sync'] = info
    def resize():
        if is_catalog:
            out['returned_count'] = len(out[key])
            out['next_offset'] = (int(result.get('offset', 0)) + len(out[key])
                                  if len(out[key]) < len(items) else result.get('next_offset'))
        else:
            ids = {c['id'] for c in out[key]}
            for detail in ('bindings', 'events'):
                if detail in result:
                    out[detail] = [v for v in result[detail] if v.get('control') in ids]
            out['returned_control_count'] = len(out[key])
            out['returned_binding_count'] = len(out.get('bindings', []))
            out['controls_truncated'] = bool(result.get('controls_truncated') or len(out[key]) < len(items))
            out['next_control_offset'] = (int(result.get('control_offset', 0)) + len(out[key])
                if out['controls_truncated'] and not result.get('control_id') else None)
    resize()
    while out[key] and len(json.dumps(out, ensure_ascii=False)) > limit - 350:
        out[key].pop()
        resize()
    if items and not out[key]:
        out['next_offset' if is_catalog else 'next_control_offset'] = None
        out['_pagination_required'] = True
        out['message'] = '单条记录超过上下文预算；请指定文件并用 area=source 分页，不要重复当前页。'
    return out


def _member_outline(items) -> list:
    """Remove member source while retaining the object/member tree."""
    outline = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        clean = {
            key: item[key]
            for key in ("name", "itemType", "kind")
            if key in item
        }
        nested = _member_outline(item.get("members"))
        if nested:
            clean["members"] = nested
        outline.append(clean)
    return outline


def _plc_read_result_for_context(
    args: dict,
    result,
    limit: int = PLC_READ_RESULT_CAP,
):
    """Keep normal PLC source pages intact; request pagination for huge pages.

    Unlike ``cap_tool_result``, this never splices the head and tail of source
    code together.  A page that cannot safely fit is replaced with an explicit
    structured retry instruction so the model reads a smaller contiguous page.
    """
    if not isinstance(result, dict):
        return cap_tool_result(result)
    result = _plc_context_path_identity(result)
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= limit:
        return result

    area = str(args.get("area") or result.get("area") or "all")
    start_line = max(1, int(args.get("start_line") or 1))
    requested_lines = max(1, int(args.get("max_lines") or 120))
    name = str(args.get("name") or result.get("name") or "")
    saved_source_path = str(args.get("source_path") or result.get("source_path") or "")
    path = str(args.get("path") or result.get("path") or "")
    if result.get("path_kind") == "saved_source":
        path = ""
    method = str(args.get("method") or "")
    suggestions = []
    areas = (
        [area] if area in {"declaration", "implementation", "members"}
        else [key for key in ("declaration", "implementation") if result.get(key)]
    )
    for code_area in areas or ["declaration", "implementation"]:
        text = str(result.get(code_area) or "")
        line_count = max(1, text.count("\n") + 1)
        chars_per_line = max(1.0, len(text) / line_count)
        # Leave room for tool metadata and the next model response.  Separate
        # declaration/implementation reads can each use most of the PLC budget.
        safe_lines = max(5, int((limit * 0.72) / chars_per_line))
        read = {
            "name": name,
            "area": code_area,
            "start_line": start_line,
            "max_lines": min(requested_lines, safe_lines, 500),
        }
        if path:
            read["path"] = path
        elif saved_source_path:
            read["source_path"] = saved_source_path
        if method:
            read["method"] = method
        suggestions.append(read)

    return {
        "name": result.get("name") or name,
        "path": result.get("path") or path,
        "source_path": saved_source_path if result.get("path_kind") == "saved_source" else result.get("source_path", ""),
        "path_kind": result.get("path_kind", ""),
        "tree_path": result.get("tree_path", "") if result.get("path_kind") != "saved_source" else "",
        "itemType": result.get("itemType"),
        "area": result.get("area") or area,
        "ranges": result.get("ranges") or {},
        "methods": _member_outline(result.get("methods")),
        "memberCodeIncluded": bool(result.get("memberCodeIncluded")),
        "_pagination_required": True,
        "original_chars": len(raw),
        "message": (
            "本次 plc_read 代码页过大，未进行首尾拼接压缩。"
            "请按 suggested_reads 分开读取声明区/实现区或缩小行范围。"
        ),
        "suggested_reads": suggestions,
    }


def _plc_context_path_identity(result: dict) -> dict:
    """Hide the legacy ambiguous ``path`` from model-facing disk results."""
    if not isinstance(result, dict):
        return result
    # Catalogs carry their identity on each item, not on the envelope.  Walk
    # the collection before classifying the root; otherwise a disk-index
    # catalog would incorrectly receive an empty root source_path and retain
    # the ambiguous item-level ``path`` fields.
    if isinstance(result.get("items"), list):
        safe = dict(result)
        safe["items"] = [
            _plc_context_path_identity(item) if isinstance(item, dict) else item
            for item in result["items"]
        ]
        return safe
    if result.get("source") in {"disk", "disk_index"} or result.get("path_kind") == "saved_source":
        safe = dict(result)
        source_path = str(safe.get("source_path") or safe.get("path") or "")
        safe["source_path"] = source_path
        safe["path_kind"] = "saved_source"
        safe["tree_path"] = ""
        safe["tree_path_available"] = False
        safe["write_path_available"] = False
        safe.pop("path", None)
        safe["next_action"] = (
            "这是已保存源码索引路径 source_path，不是 COM 写入路径。"
            "需要写入或实时回读时先用 plc_find 获取 path=TIPC^… 的精确对象树路径。"
        )
        return safe
    if isinstance(result.get("results"), list):
        safe = dict(result)
        safe["results"] = [_plc_context_path_identity(item) if isinstance(item, dict) else item
                            for item in result["results"]]
        return safe
    return result


def tool_result_for_context(name, args, result, state=None, limit=None):
    from tc_agent.source_delivery import SOURCE_READ_TOOLS
    if name in SOURCE_READ_TOOLS and isinstance(result, dict):
        # Explicit tool pagination stays truthful; no second implicit cap.
        from tc_agent.plc_path_contract import model_paths
        return model_paths(_plc_context_path_identity(result))
    value = _tool_result_for_context(name, args, result, state, limit)
    if name in {'plc_read', 'plc_read_smart', 'plc_read_fast', 'plc_read_current',
                'plc_find', 'plc_source_catalog', 'plc_structure', 'plc_tree'}:
        from tc_agent.plc_path_contract import model_paths
        return model_paths(value)
    return value


def _tool_result_for_context(
    name: str,
    args: dict,
    result,
    state: dict | None = None,
    limit: int | None = None,
):
    """Keep full tool output in UI, but store only task-relevant prompt data."""
    if name == 'plc_preflight':
        from tc_agent.preflight_summary import summarize
        return summarize(result, limit or DIAGNOSTIC_RESULT_CAP)
    if isinstance(result, dict) and ('member_baseline' in result or 'document_baseline' in result):
        # Hash baseline is small and must survive source pagination/compaction.
        return {k: v for k, v in result.items() if k != 'member_tree'}
    if isinstance(result, dict) and isinstance(result.get('recovery'), dict):
        result = {**result, 'recovery': {k: v for k, v in result['recovery'].items() if k != 'history'}}
    if name in {'plc_diagnostics', 'plc_build', 'plc_verify', 'plc_review',
                'plc_static_analysis', 'tc_hmi_diagnostics', 'tc_hmi_validate', 'tc_hmi_build'}:
        return cap_tool_result(result, limit or DIAGNOSTIC_RESULT_CAP)
    if name in {"docs_search", "docs_read"}:
        return docsearch.context_summary(name, args, result, state)
    if name == 'tc_hmi_control_schema' and isinstance(result, dict) and isinstance(result.get('attributes'), list) and not args.get('attribute'):
        # Preserve the entire name directory, not the first four inherited fields.
        # The caller can query one exact attribute for its detailed value schema.
        return {k: v for k, v in result.items() if k != 'attributes'} | {
            'attribute_names': [a['name'] for a in result['attributes']],
            'attribute_count': len(result['attributes']),
            'next_action': 'Use an exact attribute_names entry for value Schema; do not guess spellings.'}
    if name in {"plc_read", "plc_read_fast", "plc_read_smart"}:
        return _plc_read_result_for_context(args, result)
    if name == "plc_build_status" and isinstance(result, dict):
        # The approval token is an opaque capability.  Keep every token and
        # its bound action in model context while omitting bulky raw bridge
        # payloads; a generic cap is allowed to discard unknown keys and would
        # make the next exact build call impossible.
        fields = (
            "status", "verified", "pid", "solution", "project_scope", "busy",
            "commands", "plc_login_states", "login_state_verified",
            "state_fingerprint", "action", "build_plan_token", "plans",
            "not_executed", "reason", "next_action", "recovery_exhausted", "recovery",
        )
        safe = {key: result[key] for key in fields if key in result}
        return safe if len(json.dumps(safe, ensure_ascii=False)) <= TOOL_RESULT_CAP else {
            key: safe[key] for key in ("status", "verified", "pid", "solution",
                                       "commands", "state_fingerprint", "action",
                                       "build_plan_token", "plans", "reason",
                                       "next_action", "recovery_exhausted", "recovery") if key in safe
        }
    if name == "plc_source_catalog" and isinstance(result, dict):
        return _plc_context_path_identity(result)
    if name == "tc_hmi_read":
        return _hmi_read_result_for_context(args, result)
    if name in {"tc_hmi_read_smart", "tc_hmi_source_catalog"}:
        return _hmi_index_result_for_context(args, result)
    if name == 'tc_hmi_framework_packages' and isinstance(result, dict) and isinstance(result.get('packages'), list):
        # Keep the actual archive path visible to the model. Generic deep
        # summaries used to discard it and encouraged fabricated cache paths.
        summary = {k: result[k] for k in ('status', 'project', 'target_framework', 'readonly') if k in result}
        summary.update(packages=[], total_packages=len(result['packages']), packages_truncated=False)
        for package in result['packages']:
            entry = {k: package[k] for k in ('id', 'version', 'package_path', 'package_exists', 'archive_status', 'framework_inspection_applicable', 'consistent') if k in package}
            if not package.get('package_exists'):
                entry['package_candidates'] = package.get('package_candidates', [])
            summary['packages'].append(entry)
            if len(json.dumps(summary, ensure_ascii=False)) > 5500:
                summary['packages'].pop()
                summary['packages_truncated'] = True
                summary['next_action'] = '使用 package_id 精确筛选；不要猜测被省略的包路径。'
                break
        summary['returned_packages'] = len(summary['packages'])
        return summary
    return cap_tool_result(result, limit or TOOL_RESULT_CAP)


def compact_messages(messages: list, limit: int | None = None) -> tuple[list, int]:
    """把一段历史里所有过长的工具结果就地压缩。返回(新列表, 省下的字符数)。"""
    saved = 0
    out = []
    doc_state: dict = {}
    for m in messages:
        if m.get("role") == "tool":
            before = len(json.dumps(m.get("result"), ensure_ascii=False))
            capped = tool_result_for_context(
                str(m.get("name") or ""), {}, m.get("result"), doc_state,
                limit=limit,
            )
            after = len(json.dumps(capped, ensure_ascii=False))
            saved += max(0, before - after)
            m = {**m, "result": capped}
        out.append(m)
    return out, saved


def _msg_chars(m) -> int:
    return len(json.dumps(m, ensure_ascii=False))


def trim_to_budget(messages: list, budget_chars: int) -> list:
    """滑动窗口:发给模型的上下文超预算时,按【轮次边界】(user 消息)裁掉最老的
    几轮,保留能放进预算的最近若干轮。以 user 消息开头 → 不会在开头留孤儿
    tool_use。预算以字符估算(粗略 1 token≈1.5~4 字符,取字符更保守)。"""
    from tc_agent.context_partitions import project
    messages = project(messages)
    total = sum(_msg_chars(m) for m in messages)
    if total <= budget_chars:
        return messages
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(starts) <= 1:
        return messages                      # 只有一轮,无法再裁
    for k in range(1, len(starts)):          # 从第 2 轮开始找能放下的最长后缀
        suffix = messages[starts[k]:]
        if sum(_msg_chars(m) for m in suffix) <= budget_chars:
            return suffix
    return messages[starts[-1]:]             # 连最后一轮都超 → 只留最后一轮(尽力)


def _ensure_tool_results(history: list) -> list:
    """净化历史,修掉会让 Anthropic 报 400 的两类脏数据(OpenAI 宽容,Anthropic 严格):
    1. 空内容 assistant 消息(既无文字又无工具调用)→ 丢弃(messages must have non-empty content)
    2. 孤儿 tool_use(工具调用没有对应结果)→ 按 tool_calls 顺序补占位结果。"""
    out: list = []
    i = 0
    while i < len(history):
        m = history[i]
        # 丢弃空 assistant(模型偶发返回空响应,存进历史后 Anthropic 会拒)
        if m.get("role") == "assistant" and not (m.get("text") or "").strip() \
                and not m.get("tool_calls"):
            i += 1
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            have: dict = {}
            while j < len(history) and history[j].get("role") == "tool":
                have[history[j].get("id")] = history[j]
                j += 1
            for tc in m["tool_calls"]:                 # 按 tool_calls 的顺序补齐
                tid = tc["id"]
                out.append(have.get(tid) or {
                    "role": "tool", "id": tid, "name": tc.get("name", ""),
                    "result": {"aborted": "该工具调用未产生结果(已跳过)"}})
            i = j
        else:
            i += 1
    return out


# --- 附件视觉块:中间格式 {kind:image|pdf, media_type, data_b64, name} → 各家线格式 ---
# 只在【本轮】把视觉块挂到最后一条 user 消息上,不进持久历史(base64 太大,会撑爆预算)。
def _openai_attach_visual(messages: list, blocks: list) -> None:
    parts = []
    for b in blocks:
        if b.get("kind") == "image":
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{b['media_type']};base64,{b['data_b64']}"}})
        # PDF:OpenAI 兼容 chat 无通用图文块,正文已抽进 user text,这里跳过。
    if not parts:
        return
    for m in reversed(messages):
        if m.get("role") == "user":
            txt = m.get("content") if isinstance(m.get("content"), str) else ""
            m["content"] = ([{"type": "text", "text": txt}] if txt else []) + parts
            return


def _anthropic_attach_visual(messages: list, blocks: list) -> None:
    parts = []
    for b in blocks:
        if b.get("kind") == "image":
            parts.append({"type": "image", "source": {"type": "base64",
                          "media_type": b["media_type"], "data": b["data_b64"]}})
        elif b.get("kind") == "pdf":
            parts.append({"type": "document", "source": {"type": "base64",
                          "media_type": "application/pdf", "data": b["data_b64"]}})
    if not parts:
        return
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                m["content"] = content + parts
            else:
                m["content"] = [{"type": "text", "text": content or ""}] + parts
            return


def assistant_message(step: dict) -> dict:
    """Persist protocol continuation data alongside the neutral message."""
    message = {"role": "assistant", "text": step.get("text") or "",
               "tool_calls": step.get("tool_calls") or [],
               "reasoning_content": step.get("reasoning_content") or ""}
    if step.get("responses_state"):
        message["responses_state"] = step["responses_state"]
    return message


class Provider:
    protocol = "?"

    def __init__(self, base_url: str, api_key: str, model: str,
                 proxy: str | None = None, thinking: str = "auto"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.proxy = proxy or None      # 空串归一为 None(= 不走代理)
        self.thinking = thinking if thinking in ("auto", "off", "on") else "auto"

    def complete(self, system: str, history: list, tools: list) -> dict:
        raise NotImplementedError


class OpenAIProvider(Provider):
    protocol = "openai"

    def _request_options(self) -> dict:
        """DeepSeek-specific controls; other OpenAI-compatible APIs stay untouched."""
        if "api.deepseek.com" not in self.base_url.lower():
            return {}
        if self.thinking == "off":
            return {"thinking": {"type": "disabled"}}
        if self.thinking == "on":
            return {"thinking": {"type": "enabled"}}
        return {}

    def _request_body(self, messages: list, tools: list, *, stream: bool) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
        }
        # Several OpenAI-compatible gateways reject tool_choice when the
        # request has no tools (the context summarizer intentionally has none).
        if tools:
            body.update({"tools": tools, "tool_choice": "auto"})
        if stream:
            body.update({"stream": True, "stream_options": {"include_usage": True}})
        body.update(self._request_options())
        return body

    def _messages(self, system: str, history: list) -> list:
        history = _ensure_tool_results(history)
        out = [{"role": "system", "content": system}]
        for m in history:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                a = {"role": "assistant", "content": m.get("text") or ""}
                # DeepSeek thinking-mode tool calls require this field to be
                # replayed verbatim on the following request.
                if m.get("reasoning_content"):
                    a["reasoning_content"] = m["reasoning_content"]
                if m.get("tool_calls"):
                    a["tool_calls"] = [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": json.dumps(tc["args"], ensure_ascii=False)},
                    } for tc in m["tool_calls"]]
                out.append(a)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["id"],
                            "content": json.dumps(m["result"], ensure_ascii=False)})
        return out

    def complete(self, system: str, history: list, tools: list, extra_user_content=None) -> dict:
        wire_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"],
        }} for t in tools]
        msgs = self._messages(system, history)
        if extra_user_content:
            _openai_attach_visual(msgs, extra_user_content)
        resp = _post_json(self.base_url + "/chat/completions",
                          {"Authorization": f"Bearer {self.api_key}"},
                          self._request_body(msgs, wire_tools, stream=False),
                          proxy=self.proxy)
        msg = resp["choices"][0]["message"]
        calls = []
        for tc in (msg.get("tool_calls") or []):
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc["id"], "name": tc["function"]["name"], "args": args})
        u = resp.get("usage", {})
        return {"text": msg.get("content") or "", "tool_calls": calls,
                "reasoning_content": msg.get("reasoning_content") or "",
                "usage": {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}}

    def complete_stream(self, system: str, history: list, tools: list, on_delta,
                        extra_user_content=None, on_activity=None) -> dict:
        wire_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"],
        }} for t in tools]
        msgs = self._messages(system, history)
        if extra_user_content:
            _openai_attach_visual(msgs, extra_user_content)
        text, reasoning, calls, usage = "", "", {}, {"in": 0, "out": 0}
        for _ev, chunk in _post_sse(self.base_url + "/chat/completions",
                                    {"Authorization": f"Bearer {self.api_key}"},
                                    self._request_body(msgs, wire_tools, stream=True),
                                    proxy=self.proxy):
            for ch in (chunk.get("choices") or []):
                delta = ch.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]
                    if on_activity:
                        on_activity("reasoning")
                if delta.get("content"):
                    text += delta["content"]
                    on_delta(delta["content"])
                for tcd in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tcd.get("index", 0), {"id": "", "name": "", "args": ""})
                    if tcd.get("id"):
                        slot["id"] = tcd["id"]
                    fn = tcd.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
            if chunk.get("usage"):
                u = chunk["usage"]
                usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
        tool_calls = []
        for idx in sorted(calls):
            s = calls[idx]
            try:
                args = json.loads(s["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": s["id"] or f"call_{idx}", "name": s["name"], "args": args})
        return {"text": text, "tool_calls": tool_calls,
                "reasoning_content": reasoning, "usage": usage}


class AnthropicProvider(Provider):
    protocol = "anthropic"
    MAX_TOKENS = 4096

    def _messages(self, history: list) -> list:
        """翻译成 Anthropic 格式。关键:连续的 tool 结果要合并进同一条 user 消息
        (每个 assistant 的 N 个 tool_use 必须由一条含 N 个 tool_result 的 user 应答)。"""
        history = _ensure_tool_results(history)
        out: list = []
        i = 0
        while i < len(history):
            m = history[i]
            if m["role"] == "user":
                out.append({"role": "user", "content": [{"type": "text", "text": m["text"]}]})
                i += 1
            elif m["role"] == "assistant":
                content = []
                if m.get("text"):
                    content.append({"type": "text", "text": m["text"]})
                for tc in m.get("tool_calls") or []:
                    content.append({"type": "tool_use", "id": tc["id"],
                                    "name": tc["name"], "input": tc["args"]})
                out.append({"role": "assistant", "content": content})
                i += 1
            elif m["role"] == "tool":
                blocks = []
                while i < len(history) and history[i]["role"] == "tool":
                    t = history[i]
                    blocks.append({"type": "tool_result", "tool_use_id": t["id"],
                                   "content": json.dumps(t["result"], ensure_ascii=False)})
                    i += 1
                out.append({"role": "user", "content": blocks})
        return out

    def complete(self, system: str, history: list, tools: list, extra_user_content=None) -> dict:
        wire_tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["parameters"]} for t in tools]
        msgs = self._messages(history)
        if extra_user_content:
            _anthropic_attach_visual(msgs, extra_user_content)
        resp = _post_json(self.base_url + "/v1/messages",
                          {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                          {"model": self.model, "system": system, "max_tokens": self.MAX_TOKENS,
                           "messages": msgs, "tools": wire_tools}, proxy=self.proxy)
        text, calls = "", []
        for block in resp.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "name": block["name"],
                              "args": block.get("input", {})})
        u = resp.get("usage", {})
        return {"text": text, "tool_calls": calls,
                "usage": {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}}

    def complete_stream(self, system: str, history: list, tools: list, on_delta,
                        extra_user_content=None, on_activity=None) -> dict:
        wire_tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["parameters"]} for t in tools]
        msgs = self._messages(history)
        if extra_user_content:
            _anthropic_attach_visual(msgs, extra_user_content)
        text, blocks, usage = "", {}, {"in": 0, "out": 0}
        for _ev, obj in _post_sse(self.base_url + "/v1/messages",
                                  {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                                  {"model": self.model, "system": system, "max_tokens": self.MAX_TOKENS,
                                   "messages": msgs, "tools": wire_tools,
                                   "stream": True}, proxy=self.proxy):
            t = obj.get("type")
            if t == "message_start":
                usage["in"] = obj.get("message", {}).get("usage", {}).get("input_tokens", 0)
            elif t == "content_block_start":
                cb = obj.get("content_block", {})
                if cb.get("type") == "tool_use":
                    blocks[obj["index"]] = {"type": "tool_use", "id": cb["id"],
                                            "name": cb["name"], "json": ""}
            elif t == "content_block_delta":
                d = obj.get("delta", {})
                if d.get("type") == "text_delta":
                    text += d["text"]
                    on_delta(d["text"])
                elif d.get("type") == "input_json_delta":
                    b = blocks.setdefault(obj["index"], {"type": "tool_use", "id": "",
                                                         "name": "", "json": ""})
                    b["json"] += d.get("partial_json", "")
            elif t == "message_delta":
                if obj.get("usage", {}).get("output_tokens"):
                    usage["out"] = obj["usage"]["output_tokens"]
        tool_calls = []
        for idx in sorted(blocks):
            b = blocks[idx]
            try:
                args = json.loads(b["json"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": b["id"], "name": b["name"], "args": args})
        return {"text": text, "tool_calls": tool_calls, "usage": usage}


def make_provider(protocol: str, base_url: str, api_key: str, model: str,
                  proxy: str | None = None, thinking: str = "auto") -> Provider:
    from .openai_responses import ResponsesProvider
    cls = {"openai": OpenAIProvider, "anthropic": AnthropicProvider,
           "responses": ResponsesProvider}.get(protocol)
    if cls is None:
        raise ValueError(f"未知协议: {protocol}")
    return cls(base_url, api_key, model, proxy, thinking)


# ======================================================================
#  权限门:执行每个工具前,按模式 + readonly/danger 判定
#
#    plan   —— 只读照跑;写/运行时一律不执行,让模型改为输出方案
#    ask    —— 只读放行;写/运行时回调审批
#    accept —— 只读 + 改代码放行;运行时破坏性(danger=runtime)仍需审批
#    auto   —— 全放行
#  approve: Callable(name, args) -> bool;None 视为拒绝(除非 auto/只读)
# ======================================================================
MODES = ("plan", "ask", "accept", "auto")
_EDIT_CATEGORIES = {"改代码", "版本", "项目"}


def decide(mode: str, name: str) -> str:
    """只判定不执行,返回 'allow' | 'deny' | 'ask'。供异步宿主(backend)用:
    'ask' 由宿主走自己的审批通道(如 WebSocket 往返),不阻塞事件循环。"""
    t = _BY_NAME.get(name)
    if t is None:
        return "deny"
    if t["readonly"]:
        return "allow"
    if mode == "plan":
        return "deny"
    if mode == "auto":
        return "ask" if t.get("danger") else "allow"
    if mode == "accept":
        return "allow" if t.get("category") in _EDIT_CATEGORIES and not t.get("danger") else "ask"
    return "ask"


def gate(mode: str, name: str, args: dict, approve) -> tuple[bool, str]:
    t = _BY_NAME.get(name)
    if t is None:
        return False, f"未知工具: {name}"
    if t["readonly"]:
        return True, ""
    if mode == "plan":
        return False, "plan 模式:只规划不执行，请改为给出方案而不是调用该工具"
    if mode == "auto":
        if not t.get("danger"):
            return True, ""
        if approve is None:
            return False, "自动（受保护）模式下高风险操作仍需审批"
        ok = bool(approve(name, args))
        return ok, "" if ok else "用户拒绝了此操作"
    if mode == "accept" and t.get("category") in _EDIT_CATEGORIES and not t.get("danger"):
        return True, ""                       # accept: 改代码放行
    # ask 模式的全部写操作,或 accept 模式的运行时破坏性操作 → 审批
    if approve is None:
        return False, "无审批通道，已拒绝"
    ok = bool(approve(name, args))
    return ok, "" if ok else "用户拒绝了此操作"


# ======================================================================
#  agent 循环(厂商无关)
# ======================================================================
def run(user_text: str, provider: Provider, *, mode: str = "auto", approve=None,
        max_steps: int = 8, verbose: bool = True) -> str:
    history: list = [{"role": "user", "text": user_text}]
    schema = tools_schema()
    total_in = total_out = 0
    for step in range(max_steps):
        st = provider.complete(SYSTEM_PROMPT + selected_contract_prompt(schema, tool_metadata), history, schema)
        total_in += st["usage"]["in"]
        total_out += st["usage"]["out"]
        history.append(assistant_message(st))
        if not st["tool_calls"]:
            if verbose:
                print(f"\n[{provider.protocol}/{mode} · {step + 1} 轮 · tokens "
                      f"{total_in}/{total_out}]", file=sys.stderr)
            return st["text"]
        batch_gate_blocked = False
        for tc in st["tool_calls"]:
            invalid = validate_tool_arguments(tc['name'], tc['args'])
            if invalid is not None:
                history.append({'role': 'tool', 'id': tc['id'], 'name': tc['name'], 'result': invalid})
                continue
            allowed, reason = gate(mode, tc["name"], tc["args"], approve)
            if verbose:
                mark = "⚙" if allowed else "⛔"
                print(f"  {mark} {tc['name']}({tc['args']})"
                      f"{'' if allowed else ' — ' + reason}", file=sys.stderr)
            if batch_gate_blocked and not _BY_NAME.get(tc["name"], {}).get("readonly", False):
                result = blocked_batch_result()
            else:
                result = run_tool(tc["name"], tc["args"]) if allowed else {"denied": reason}
            batch_gate_blocked = batch_gate_blocked or gate_rejected(result)
            history.append({"role": "tool", "id": tc["id"], "name": tc["name"], "result": result})
    return "(达到最大轮数,未产出最终回答)"


# ======================================================================
#  spike 入口:从 config 取 DeepSeek 档,按 --proto 选端点
# ======================================================================
def _spike_provider(protocol: str) -> Provider:
    for p in cfgmod.load_config()["providers"]:
        if p.get("kind") == "api" and p.get("api_key") and "deepseek" in (
                (p.get("base_url", "") + p.get("name", "")).lower()):
            key = p["api_key"]
            model = p.get("model") or "deepseek-chat"
            # DeepSeek 同时提供两种兼容端点,同一个 key。
            base = "https://api.deepseek.com/anthropic" if protocol == "anthropic" \
                else "https://api.deepseek.com"
            return make_provider(protocol, base, key, model)
    raise SystemExit("config 里没有可用的 DeepSeek Provider(需含 api_key)")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    args = sys.argv[1:]
    protocol, mode = "openai", "auto"
    if "--proto" in args:
        i = args.index("--proto"); protocol = args[i + 1]; del args[i:i + 2]
    if "--mode" in args:
        i = args.index("--mode"); mode = args[i + 1]; del args[i:i + 2]

    def console_approve(name: str, tool_args: dict) -> bool:
        try:
            ans = input(f"\n?? 允许执行 {name}({tool_args})? [y/N] ").strip().lower()
        except EOFError:
            return False
        return ans in ("y", "yes")

    q = " ".join(args) or "帮我确认一下 XAE 连接状态"
    print(f"[user · {protocol}/{mode}] {q}\n", file=sys.stderr)
    print(run(q, _spike_provider(protocol), mode=mode,
              approve=console_approve if mode in ("ask", "accept") else None))


if __name__ == "__main__":
    main()
