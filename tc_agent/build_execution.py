"""Approval-bound PLC build execution.

The build operation is intentionally separate from diagnostics and preflight:
those tools only observe state, while a build changes XAE/compiler state and
must be approved against a fresh, exact snapshot.  The plan token is issued
only from live bridge observations and is kept in this backend process; a
model-supplied or stale token can never authorize a new build.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
import time
import uuid

from tc_template._ps_bridge import _TOOL_TARGET_PID, ps_com
from tc_template.plc_build_diagnostics import build_diagnostics


TOKEN_TTL_SECONDS = 120.0


class BuildPlanError(ValueError):
    """A build plan is missing, stale, or no longer matches live XAE state."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(str(result.get("error") or result.get("reason") or "build plan invalid"))


@dataclass(frozen=True)
class _BuildPlan:
    token: str
    action: str
    pid: int
    solution: str
    project_scope: object
    fingerprint: str
    issued_at: float
    used: bool = False


_PLANS: dict[str, _BuildPlan] = {}
_LOCK = threading.RLock()
_BUILD_LOCKS: dict[tuple[int, str], threading.Lock] = {}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _path(value) -> str:
    return str(value or "").strip().replace("/", "\\").casefold()


def _safe_call(command: str, **args):
    try:
        return ps_com(command, timeout=30.0, **args)
    except Exception as exc:  # noqa: BLE001 - surfaced as unavailable evidence
        return {"status": "unavailable", "error": str(exc),
                "command": command, "not_executed": True}


def _command_available(build_state: dict, action: str) -> bool | None:
    commands = build_state.get("commands")
    if not isinstance(commands, dict):
        return None
    item = commands.get(action)
    if not isinstance(item, dict):
        return None
    value = item.get("available")
    return value if isinstance(value, bool) else None


def _runtime_states(runtimes: dict, requested: str) -> tuple[list[dict], bool]:
    if not isinstance(runtimes, dict) or runtimes.get("status") in {"unavailable", "error"}:
        return [], False
    items = list(runtimes.get("plcs") or []) if isinstance(runtimes, dict) else []
    requested = str(requested or "").strip()
    if requested:
        items = [item for item in items
                 if str(item.get("name") or "").casefold() == requested.casefold()]
        if not items:
            return [], False
    states: list[dict] = []
    complete = True
    for item in items:
        name = str(item.get("name") or "")
        state = _safe_call("plc-online-state", runtime=name)
        if not isinstance(state, dict) or state.get("status") == "unavailable":
            complete = False
            states.append({"name": name, "status": "unavailable",
                           "error": (state or {}).get("error") if isinstance(state, dict) else str(state)})
            continue
        if not isinstance(state.get("logged_in"), bool):
            complete = False
        states.append({
            "name": name,
            "ads_port": item.get("ads_port"),
            "logged_in": state.get("logged_in"),
            "operation_state": state.get("operation_state"),
            "application_state": state.get("application_state"),
            "source": state.get("source"),
        })
    # A project without a PLC runtime is still a valid local-build scope.  It
    # is not an unverified login state; there simply is no runtime to inspect.
    return states, complete


def capture_build_state(args: dict | None = None) -> dict:
    """Capture only live, read-only build/login/XAE state.

    No command in this function can log in, log out, stop, start, activate, or
    compile.  A plan token is emitted only when all identity and command-state
    fields needed for a safe approval are present.
    """
    args = dict(args or {})
    requested_runtime = str(args.get("runtime") or "").strip()
    connect = _safe_call("connect-check")
    build_state = _safe_call("build-state")
    project = _safe_call("project-info")
    runtimes = _safe_call("plc-runtimes")
    # BuildSolution/RebuildSolution affects all PLC projects, not only the
    # runtime named by a caller.
    login_states, login_complete = _runtime_states(runtimes, "")

    pid = int((connect or {}).get("pid") or _TOOL_TARGET_PID.get() or 0) if isinstance(connect, dict) else int(_TOOL_TARGET_PID.get() or 0)
    solution = str((connect or {}).get("solution") or (project or {}).get("solution") or "").strip() if isinstance(connect, dict) else ""
    if not solution and isinstance(project, dict):
        solution = str(project.get("solution") or "").strip()
    project_scope = {
        "solution": _path(solution),
        "plc_projects": (project.get("plc_projects") if isinstance(project, dict) else None),
        "project_count": (project.get("project_count") if isinstance(project, dict) else None),
    }
    project_binding_verified = (
        isinstance(project, dict)
        and isinstance(project.get("plc_projects"), list)
        and bool(project.get("plc_projects"))
    )
    busy = build_state.get("busy") if isinstance(build_state, dict) else None
    commands = build_state.get("commands") if isinstance(build_state, dict) else None
    command_states = {
        action: (commands.get(action) if isinstance(commands, dict) else None)
        for action in ("build", "rebuild")
    }
    identity_ok = bool(pid and solution and project_binding_verified
                      and isinstance(build_state, dict)
                      and build_state.get("status") not in {"unavailable", "error"}
                      and isinstance(project, dict)
                      and project.get("status") not in {"unavailable", "error"})
    for source in (connect, build_state, project, runtimes):
        if isinstance(source, dict):
            if source.get("solution") and _path(source["solution"]) != _path(solution):
                identity_ok = False
            if source.get("pid") and int(source["pid"]) != pid:
                identity_ok = False
    busy_known = isinstance(busy, bool)
    command_known = all(isinstance(_command_available(build_state, action), bool)
                        for action in ("build", "rebuild"))
    fingerprint_payload = {
        "pid": pid,
        "solution": _path(solution),
        "project_scope": project_scope,
        "build_state": build_state,
        "login_states": login_states,
    }
    fingerprint = hashlib.sha256(_json(fingerprint_payload).encode("utf-8")).hexdigest()
    common = {
        "status": "ready" if identity_ok and busy_known and command_known and login_complete and not busy else "incomplete",
        "verified": identity_ok and busy_known and command_known and login_complete,
        "pid": pid,
        "solution": solution,
        "project_scope": project_scope,
        "build_state": build_state,
        "busy": busy,
        "commands": command_states,
        "plc_login_states": login_states,
        "login_state_verified": login_complete,
        "state_fingerprint": fingerprint,
        "not_executed": True,
    }
    if not identity_ok:
        common.update(status="unavailable", verified=False,
                      reason="无法同时确认绑定 XAE、解决方案、项目和构建状态。",
                      next_action="保持当前 XAE/PLC 不变，重新执行 plc_build_status；不要直接调用 plc_build。")
        return common
    if not busy_known or not command_known or not login_complete:
        common.update(status="incomplete", verified=False,
                      reason="构建占用、Build/Rebuild 命令状态或 PLC 登录状态不完整。",
                      next_action="补齐只读状态证据；不得用 expected_state 或历史结果代替。")
        return common
    if busy:
        common.update(status="busy", verified=False,
                      reason="XAE 当前已有构建占用。",
                      next_action="等待当前构建结束后重新读取 plc_build_status；不要重复触发构建。")
        return common

    online = [item['name'] for item in login_states if item.get('logged_in') is True]
    if online:
        common.update(status="blocked", verified=False, online_runtimes=online,
                      reason="编译前必须退出所有受影响 PLC 工程的在线登录。",
                      next_action="先申请 tc_logout 审批，登出这些 PLC 编辑器后重新读取 plc_build_status；不停止 PLC、不切 Config，也不以 Rebuild 绕过。")
        return common

    requested_action = str(args.get("action") or "").strip().lower()
    candidate_actions = ((requested_action,) if requested_action in {"build", "rebuild"}
                         else ("build", "rebuild"))
    plans = {}
    for action in candidate_actions:
        if _command_available(build_state, action) is not True:
            continue
        token = uuid.uuid4().hex
        with _LOCK:
            for old_token, old_plan in list(_PLANS.items()):
                if time.monotonic() - old_plan.issued_at > TOKEN_TTL_SECONDS:
                    _PLANS.pop(old_token, None)
            _PLANS[token] = _BuildPlan(
                token=token, action=action, pid=pid, solution=_path(solution),
                project_scope=project_scope, fingerprint=fingerprint,
                issued_at=time.monotonic(),
            )
        plans[action] = {"action": action, "build_plan_token": token,
                         "state_fingerprint": fingerprint,
                         "expires_in_seconds": TOKEN_TTL_SECONDS}
    if not plans:
        common.update(status="blocked", verified=False,
                      reason="XAE 当前没有报告可用的 Build 或 Rebuild 命令。",
                      next_action="在当前 XAE 中确认命令可用后重新读取；Agent 不猜测命令状态。")
        return common
    common.update(status="ready", verified=True, plans=plans)
    if len(plans) == 1:
        only = next(iter(plans.values()))
        common.update(action=only["action"], build_plan_token=only["build_plan_token"])
    common["next_action"] = "选择 plans 中实际可用的 action，将对应 build_plan_token 原样提交给 plc_build 或 plc_verify；审批参数必须完全一致。"
    return common


def _plan_failure(status: str, reason: str, next_action: str, *, actual=None) -> dict:
    return {
        "status": status, "verified": False, "build_performed": False,
        "buildPerformed": False, "not_executed": True,
        "error_type": "build_state_conflict" if status == "conflict" else "build_plan_required",
        "reason": reason, "error": reason, "actual": actual or {},
        "next_action": next_action,
    }


def _claim_plan(args: dict, current: dict, *, consume: bool = True) -> _BuildPlan:
    token = str(args.get("build_plan_token") or "").strip()
    action = str(args.get("action") or "build").strip().lower()
    if action not in {"build", "rebuild"}:
        raise BuildPlanError(_plan_failure("blocked", "action 必须是 build 或 rebuild。",
                                           "重新读取 plc_build_status 并选择其 plans 中的 action。"))
    if not token:
        raise BuildPlanError(_plan_failure("blocked", "构建缺少只读状态产生的 build_plan_token。",
                                           "先调用 plc_build_status 获取新令牌，再申请本次 plc_build 审批；审批前不得执行构建。"))
    with _LOCK:
        plan = _PLANS.get(token)
        if plan is None:
            raise BuildPlanError(_plan_failure("conflict", "build_plan_token 未由当前后端的实时状态检查签发。",
                                               "重新调用 plc_build_status；不要编造或复用旧令牌。"))
        if plan.used or time.monotonic() - plan.issued_at > TOKEN_TTL_SECONDS:
            raise BuildPlanError(_plan_failure("conflict", "build_plan_token 已使用或已过期。",
                                               "重新调用 plc_build_status 并重新审批。"))
        if plan.action != action:
            raise BuildPlanError(_plan_failure("conflict", "请求的 action 与审批令牌绑定动作不一致。",
                                               "使用同一 plans 条目中的 action 和 build_plan_token。"))
        if int(current.get("pid") or 0) != plan.pid or _path(current.get("solution")) != plan.solution:
            raise BuildPlanError(_plan_failure("conflict", "XAE PID 或解决方案已变化，审批令牌失效。",
                                               "重新绑定目标 XAE、读取 plc_build_status 并重新审批。",
                                               actual={"pid": current.get("pid"), "solution": current.get("solution")}))
        if current.get("state_fingerprint") != plan.fingerprint:
            raise BuildPlanError(_plan_failure("conflict", "审批后 XAE/项目/登录/构建状态发生变化，构建未执行。",
                                               "重新读取 plc_build_status；不得自动重试或改变 PLC/XAE 状态。",
                                               actual={"state_fingerprint": current.get("state_fingerprint")}))
        if current.get("busy") is not False:
            raise BuildPlanError(_plan_failure("busy", "XAE 构建占用状态不再确认为空闲，构建未执行。",
                                               "等待并重新读取 plc_build_status；不要 Logout、Stop 或重复构建。",
                                               actual={"busy": current.get("busy")}))
        command = (current.get("commands") or {}).get(action)
        if not isinstance(command, dict) or command.get("available") is not True:
            raise BuildPlanError(_plan_failure("conflict", f"XAE 当前未报告 {action} 命令可用，构建未执行。",
                                               "重新读取实际 Build/Rebuild 命令状态；不要猜测替代动作。"))
        if consume:
            _PLANS[token] = _BuildPlan(**{**plan.__dict__, "used": True})
        return plan


def execute_plc_build(args: dict) -> dict:
    """Recheck a plan immediately before the sole build dispatch."""
    before = capture_build_state(args)
    if before.get("status") != "ready" or before.get("verified") is not True:
        return _plan_failure("conflict", "构建前只读状态未形成可执行计划，构建未执行。",
                             str(before.get("next_action") or "重新读取 plc_build_status。"), actual=before)
    try:
        plan = _claim_plan(args, before, consume=False)
    except BuildPlanError as exc:
        return exc.result

    lock_key = (plan.pid, plan.solution)
    with _LOCK:
        mutex = _BUILD_LOCKS.setdefault(lock_key, threading.Lock())
    if not mutex.acquire(blocking=False):
        return _plan_failure("busy", "已有构建操作占用该 XAE/解决方案，构建未执行。",
                             "等待当前构建完成后重新读取状态并重新审批。")
    try:
        # Recheck inside the resource lock, then atomically consume once.
        before = capture_build_state(args)
        try:
            plan = _claim_plan(args, before)
        except BuildPlanError as exc:
            return exc.result
        from tc_template._ps_bridge import com_build
        raw = com_build(always_read_errors=True, action=plan.action)
        result = build_diagnostics(raw)
        after = capture_build_state(args)
        result.update({
            "build_plan_token": plan.token,
            "action": plan.action,
            "approval_scope": {"pid": plan.pid, "solution": plan.solution,
                                "project_scope": plan.project_scope,
                                "state_fingerprint": plan.fingerprint},
            "pre_execution_state": before,
            "post_execution_state": after,
            "build_performed": result.get("buildPerformed", result.get("build_performed")),
            "buildPerformed": result.get("buildPerformed", result.get("build_performed")),
        })
        if (after.get("status") in {"unavailable", "incomplete"}
                or after.get("verified") is not True
                or after.get("pid") != plan.pid
                or _path(after.get("solution")) != plan.solution
                or after.get("project_scope") != plan.project_scope):
            result.update(status="incomplete", verified=False,
                          compiler_verified=False,
                          verification_status="post_build_state_incomplete",
                          next_action="构建结果已返回，但构建后 XAE 状态证据不完整；不要宣称已闭环或自动重试。")
        else:
            result["verification_status"] = "build_result_and_post_state_read"
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "uncertain", "verified": False,
            "build_performed": None, "buildPerformed": None,
            "not_executed": False, "error_type": "build_execution_uncertain",
            "error": str(exc),
            "next_action": "构建调用状态不确定；不要自动再次 Build/Rebuild、Logout 或 Stop。先读取 plc_build_status 和 plc_diagnostics。",
        }
    finally:
        mutex.release()


def verify_build_plan(args: dict) -> dict | None:
    """Validate a verify call without consuming the plan.

    ``plc_verify`` performs the claim in ``execute_plc_build`` exactly once;
    this helper only exists for tests and callers that need a preflight check.
    """
    state = capture_build_state(args)
    if state.get("status") != "ready" or state.get("verified") is not True:
        return _plan_failure("conflict", "验证前构建状态不可执行。",
                             str(state.get("next_action") or "重新读取 plc_build_status。"), actual=state)
    try:
        _claim_plan(args, state, consume=False)
    except BuildPlanError as exc:
        return exc.result
    return None


def clear_plans() -> None:
    """Test/support hook; does not touch XAE or PLC state."""
    with _LOCK:
        _PLANS.clear()
        _BUILD_LOCKS.clear()
