"""Structured, short-lived authorization plans for mutating Agent actions.

The normal tool approval prompt is intentionally ephemeral.  A plan is the
durable counterpart used when a model needs to ask for approval and the user
answers in a later message (for example, ``授权``).  This module contains no
I/O and no TwinCAT calls; it only canonicalizes action scopes and compares the
execution identity captured when a plan was proposed with the identity at
confirmation time.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import os
import time
import uuid
import re


PLAN_VERSION = 1
DEFAULT_TTL_SECONDS = 120.0
CONFIRMATIONS = frozenset({
    "授权", "同意", "确认", "可以", "允许", "批准", "approve", "approved",
    "confirm", "confirmed", "yes", "ok",
})
REJECTIONS = frozenset({
    "拒绝", "不同意", "取消", "不可以", "不允许", "deny", "denied", "no",
    "cancel", "取消授权",
})

# A combination tool is presented as the actual mutations it contains.  This
# prevents ``tc_online`` or ``tc_deploy`` from becoming a prefix that silently
# grants unrelated runtime actions.
COMPOSITE_ACTIONS: dict[str, tuple[str, ...]] = {
    "tc_online": ("tc_login", "tc_start"),
    "tc_deploy": (
        "tc_build", "tc_boot", "tc_activate", "tc_restart", "tc_login", "tc_start",
    ),
}


class AuthorizationError(ValueError):
    """A proposed or confirmed plan failed a closed-world validation."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError):
        return text.casefold()


def _scalar(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(value[key]) for key in sorted(value, key=str)}
    return str(value)


def normalize_args(args: dict | None) -> dict:
    """Canonicalize action parameters without dropping safety-relevant keys."""
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise AuthorizationError("授权动作参数必须是对象")
    return _scalar(args)


def normalize_action(name: str, args: dict | None = None) -> dict:
    tool_name = str(name or "").strip()
    if not tool_name:
        raise AuthorizationError("授权动作缺少工具名")
    normalized = {"name": tool_name, "args": normalize_args(args)}
    normalized["key"] = action_key(normalized)
    return normalized


def action_key(action: dict | str, args: dict | None = None) -> str:
    if isinstance(action, str):
        action = normalize_action(action, args)
    else:
        action = {
            "name": str(action.get("name") or ""),
            "args": normalize_args(action.get("args") or {}),
        }
    return f"{action['name']}|{_json(action['args'])}"


def expand_actions(name: str, args: dict | None = None) -> list[dict]:
    """Expand a tool call into the concrete actions it authorizes."""
    tool_name = str(name or "").strip()
    normalized = normalize_args(args)
    if tool_name not in COMPOSITE_ACTIONS:
        return [normalize_action(tool_name, normalized)]
    expanded = []
    for item in COMPOSITE_ACTIONS[tool_name]:
        # The same runtime selector must flow to every child action.  Other
        # parameters are not copied to a child unless the tool explicitly
        # exposes them; preserving the selector is enough to bind the scope.
        child_args = {
            key: normalized[key] for key in ("runtime", "ads_port", "all_plcs")
            if key in normalized
        }
        expanded.append(normalize_action(item, child_args))
    return expanded


def normalize_context(*, thread_id: str, solution: str, pid: int,
                      process_identity=None, target_netid: str = "",
                      runtimes=None, backend_session_id: str = "") -> dict:
    endpoints = []
    for item in runtimes or []:
        if not isinstance(item, dict):
            continue
        endpoint = {
            "name": str(item.get("name") or item.get("instance") or ""),
            "ads_port": int(item["ads_port"]) if item.get("ads_port") is not None else None,
        }
        # Project/runtime identity changes must invalidate a plan even when the
        # ADS port happens to be reused.
        for key in ("path", "project", "project_name", "plc_project"):
            if item.get(key):
                endpoint[key] = str(item[key])
        endpoints.append(endpoint)
    endpoints.sort(key=_json)
    return {
        "thread_id": str(thread_id or ""),
        "solution": _path(solution),
        "pid": int(pid or 0),
        "process_identity": _scalar(process_identity),
        "target_netid": str(target_netid or "").strip().casefold(),
        "runtimes": endpoints,
        "backend_session_id": str(backend_session_id or ""),
    }


def context_matches(plan: dict, current: dict, *, allow_session_change: bool = False) -> tuple[bool, str]:
    expected = plan.get("context") if isinstance(plan, dict) else None
    actual = current if isinstance(current, dict) else None
    if not expected or not actual:
        return False, "缺少授权计划或当前执行身份"
    if str(expected.get("thread_id") or "") != str(actual.get("thread_id") or ""):
        return False, "授权计划属于另一个对话"
    if _path(expected.get("solution")) != _path(actual.get("solution")):
        return False, "解决方案已变化"
    if int(expected.get("pid") or 0) != int(actual.get("pid") or 0):
        return False, "XAE PID 已变化"
    if _json(expected.get("process_identity")) != _json(actual.get("process_identity")):
        return False, "XAE 进程启动标识已变化，拒绝 PID 复用"
    if str(expected.get("target_netid") or "").casefold() != str(actual.get("target_netid") or "").casefold():
        return False, "目标 AMS NetId 已变化"
    if _json(expected.get("runtimes") or []) != _json(actual.get("runtimes") or []):
        return False, "PLC runtime/ADS 端点已变化"
    if (not allow_session_change and
            str(expected.get("backend_session_id") or "") != str(actual.get("backend_session_id") or "")):
        return False, "Agent 后端会话已变化"
    return True, ""


def _remaining_actions(plan: dict) -> list[dict]:
    consumed = Counter(plan.get("completed_action_keys") or [])
    consumed.update(plan.get("failed_action_keys") or [])
    remaining = []
    for item in plan.get("approved_actions") or plan.get("actions") or []:
        key = str(item.get("key") or action_key(item))
        if consumed[key]:
            consumed[key] -= 1
        else:
            remaining.append(item)
    return remaining


def plan_allows_action(plan: dict, name: str, args: dict | None = None) -> bool:
    """Check one call against remaining, explicitly approved concrete actions."""
    if not isinstance(plan, dict) or plan.get("status") != "authorized":
        return False
    if float(plan.get("expires_at") or 0) <= time.time():
        return False
    requested = [item["key"] for item in expand_actions(name, args)]
    available = [str(item.get("key") or action_key(item)) for item in _remaining_actions(plan)]
    return available[:len(requested)] == requested


def consume_action(plan: dict, name: str, args: dict | None = None, *, success: bool) -> dict:
    """Return a copy with one concrete call recorded; never auto-retry failures."""
    updated = deepcopy(plan)
    keys = [item["key"] for item in expand_actions(name, args)]
    field = "completed_action_keys" if success else "failed_action_keys"
    values = list(updated.get(field) or [])
    values.extend(keys)
    updated[field] = values
    all_keys = [str(item.get("key") or action_key(item))
                for item in updated.get("approved_actions") or updated.get("actions") or []]
    done = set(updated.get("completed_action_keys") or [])
    failed = set(updated.get("failed_action_keys") or [])
    if failed:
        updated["status"] = "failed"
    elif all_keys and not _remaining_actions(updated):
        updated["status"] = "completed"
        updated["completed_at"] = time.time()
    return updated


def make_plan(*, thread_id: str, solution: str, pid: int, process_identity=None,
              target_netid: str = "", runtimes=None, actions=None,
              source_request: str = "", rationale: str = "", ttl: float = DEFAULT_TTL_SECONDS,
              backend_session_id: str = "", plan_id: str = "") -> dict:
    raw_actions = actions or []
    if not isinstance(raw_actions, list) or not raw_actions:
        raise AuthorizationError("授权计划至少需要一个动作")
    expanded = []
    for item in raw_actions:
        if not isinstance(item, dict):
            raise AuthorizationError("授权计划动作必须是对象")
        expanded.extend(expand_actions(item.get("name") or item.get("tool_name"),
                                        item.get("args") or item.get("arguments") or {}))
    if not expanded:
        raise AuthorizationError("授权计划没有有效动作")
    now = time.time()
    expires = now + max(1.0, min(float(ttl), 900.0))
    return {
        "id": plan_id or f"auth-{uuid.uuid4().hex}",
        "version": PLAN_VERSION,
        "status": "pending",
        "created_at": now,
        "expires_at": expires,
        "source_request": str(source_request or "")[:4000],
        "rationale": str(rationale or "")[:2000],
        "context": normalize_context(
            thread_id=thread_id, solution=solution, pid=pid,
            process_identity=process_identity, target_netid=target_netid,
            runtimes=runtimes, backend_session_id=backend_session_id,
        ),
        "actions": expanded,
        "approved_actions": [],
        "completed_action_keys": [],
        "failed_action_keys": [],
        "confirmation_consumed_at": None,
    }


def confirmable(text: str) -> bool:
    value = "".join(str(text or "").strip().casefold().split())
    return value in CONFIRMATIONS


def rejectable(text: str) -> bool:
    value = "".join(str(text or "").strip().casefold().split())
    return value in REJECTIONS


def explicit_report_actions(text: str) -> list[dict]:
    """Extract only a strongly formatted *current* approval request.

    This is a compatibility bridge for older model prompts that wrote an
    approval question instead of calling ``tc_request_authorization``.  It is
    deliberately narrow: historical/quoted text and ordinary questions do
    not create a plan, and a negative mention does not become an action.
    """
    value = str(text or "").strip()
    lower = value.casefold()
    if re.search(r"历史|之前|旧请求|原话|引用|记录|数据库", lower):
        return []
    # Only the explicit request clause is eligible; preceding state/history
    # prose must never manufacture an action (e.g. "已登录").
    marker = re.search(r"(?:需要确认|请确认|是否授权|授权执行|确认执行|confirm|approval)", lower)
    if marker:
        lower = lower[marker.start():]
    if re.search(r"历史|之前|旧请求|原话|引用|记录|数据库", lower):
        return []
    if not re.search(r"(?:需要确认|请确认|是否授权|授权执行|确认执行|confirm|approval)", lower):
        return []
    actions: list[dict] = []

    def positive(pattern: str, negative: str) -> bool:
        return bool(re.search(pattern, lower)) and not bool(re.search(negative, lower))

    if positive(r"activate\s*configuration|激活配置", r"不(?:要|需|用)|无需|不激活"):
        actions.append({"name": "tc_activate", "args": {}})
    if positive(r"\brestart\b|重启", r"不(?:要|需|用)|无需|不重启"):
        actions.append({"name": "tc_restart", "args": {}})
    if positive(r"login\s*(?:\+|→|->|and|且)?\s*start|登录\s*(?:\+|→|->|并|且)?\s*启动",
                r"不(?:要|需|用)|无需|不登录"):
        actions.extend([
            {"name": "tc_login", "args": {}},
            {"name": "tc_start", "args": {}},
        ])
    elif positive(r"\blogin\b|登录", r"不(?:要|需|用)|无需|不登录"):
        actions.append({"name": "tc_login", "args": {}})
    elif positive(r"\bstart\b|启动", r"不(?:要|需|用)|无需|不启动"):
        actions.append({"name": "tc_start", "args": {}})
    if positive(r"\bdeploy\b|部署", r"不(?:要|需|用)|无需|不部署") and not actions:
        actions.append({"name": "tc_deploy", "args": {}})
    return actions


def select_actions(plan: dict, selection=None) -> list[dict]:
    """Resolve a UI/user partial approval to exact action keys only."""
    actions = list(plan.get("actions") or [])
    if selection is None:
        return actions
    if not isinstance(selection, list) or not selection:
        raise AuthorizationError("部分授权必须选择至少一个已列出的动作")
    wanted = {
        str((item.get("key") or action_key(item)) if isinstance(item, dict) else item)
        for item in selection
    }
    chosen = [item for item in actions if str(item.get("key") or action_key(item)) in wanted]
    if len({str(item.get("key") or action_key(item)) for item in chosen}) != len(wanted):
        raise AuthorizationError("部分授权包含计划外动作")
    if not chosen:
        raise AuthorizationError("部分授权没有匹配计划动作")
    if chosen != actions[:len(chosen)]:
        raise AuthorizationError("部分授权必须包含前置步骤；请重新提出独立计划")
    return chosen
