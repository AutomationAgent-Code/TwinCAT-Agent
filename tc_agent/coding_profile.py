"""Project-local PLC generation contract.

The profile is intentionally compact and executable: it is injected before
the model drafts code and is also returned by a read-only tool.  A project may
override the defaults in ``.TwinCATAgent/coding_profile.json`` without
changing the repository-wide coding standard.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_PROFILE: dict[str, Any] = {
    "version": 1,
    "identifier_language": "English",
    "comment_language": "Simplified Chinese",
    "author": "TwinCAT Agent",
    "indent_spaces": 4,
    "declaration_order": [
        "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR", "VAR CONSTANT",
    ],
    "require_fb_header": True,
    "transaction_status": ["bDone", "bBusy", "bError", "nErrId"],
    "require_case_state_machine": True,
    "require_transaction_timeout": True,
    "error_code_source": "GVL_ErrorCodes",
    "child_fb_call_policy": "inputs -> exactly one unconditional call per cycle -> outputs",
    "template_first": True,
    "extra_rules": [],
}


def profile_path(solution: str) -> Path | None:
    if not solution:
        return None
    return Path(solution).parent / ".TwinCATAgent" / "coding_profile.json"


def load_profile(solution: str) -> dict[str, Any]:
    profile = deepcopy(DEFAULT_PROFILE)
    path = profile_path(solution)
    if path and path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key in DEFAULT_PROFILE:
                    if key in saved:
                        profile[key] = saved[key]
        except (OSError, ValueError, TypeError):
            profile["profile_warning"] = "项目 coding_profile.json 无法解析，已使用默认规则"
    profile["path"] = str(path) if path else ""
    return profile


def save_profile(solution: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = profile_path(solution)
    if path is None:
        raise ValueError("当前 XAE 没有打开解决方案，无法保存项目编码规则")
    unknown = sorted(set(updates) - set(DEFAULT_PROFILE))
    if unknown:
        raise ValueError(f"未知 coding profile 字段: {', '.join(unknown)}")
    profile = load_profile(solution)
    profile.pop("path", None)
    profile.pop("profile_warning", None)
    profile.update(updates)
    if not isinstance(profile.get("extra_rules"), list):
        raise ValueError("extra_rules 必须是字符串数组")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    profile["path"] = str(path)
    return profile


def prompt_contract(solution: str) -> str:
    p = load_profile(solution)
    order = " → ".join(str(item) for item in p["declaration_order"])
    status = "/".join(str(item) for item in p["transaction_status"])
    extras = "；".join(str(item) for item in p.get("extra_rules") or []) or "无"
    return (
        "PLC 生成前硬约束（项目 coding profile）："
        f"标识符={p['identifier_language']}；注释={p['comment_language']}；"
        f"缩进={p['indent_spaces']}空格；声明顺序={order}；"
        f"FB中文头注释={'必须' if p['require_fb_header'] else '按项目'}；"
        f"事务状态={status}；CASE状态机={'必须' if p['require_case_state_machine'] else '按需'}；"
        f"超时={'必须' if p['require_transaction_timeout'] else '按需'}；"
        f"错误码来源={p['error_code_source']}；子FB调用={p['child_fb_call_policy']}；"
        f"模板优先={'是' if p['template_first'] else '否'}；附加规则={extras}。"
        "这些规则必须在首次生成候选代码时满足，不得先自由生成再依赖审核返工。"
    )
