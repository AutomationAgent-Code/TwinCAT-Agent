"""Provider-neutral execution envelopes for the TwinCAT Agent runtime.

The UI and model adapters intentionally keep their existing message formats.
These envelopes are the durable, internal contract used by the execution
ledger so a restarted Agent can distinguish a completed action from one whose
outcome is uncertain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tc_agent.conversation_store import ConversationStore


def canonical_json(value: Any) -> str:
    """Return stable JSON suitable for hashing and SQLite audit records."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def action_idempotency_key(
    run_id: str, tool_call_id: str, tool_name: str, arguments: dict,
) -> str:
    """Build the identity of one model-issued action within an Agent run."""
    material = canonical_json({
        "run_id": str(run_id),
        "tool_call_id": str(tool_call_id),
        "tool_name": str(tool_name),
        "arguments": arguments,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    run_id: str
    step_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict
    category: str = ""
    danger: str = ""
    readonly: bool = False
    target_pid: int = 0
    idempotency_key: str = ""

    def envelope(self) -> dict:
        payload = asdict(self)
        if not payload["idempotency_key"]:
            payload["idempotency_key"] = action_idempotency_key(
                self.run_id, self.tool_call_id, self.tool_name, self.arguments
            )
        payload["type"] = "action_request"
        payload["version"] = 1
        return payload


@dataclass(frozen=True)
class ActionResult:
    execution_id: str
    status: str
    result: Any = None
    error: str = ""
    replayed: bool = False

    def envelope(self) -> dict:
        payload = asdict(self)
        payload["type"] = "action_result"
        payload["version"] = 1
        return payload


class DurableAction:
    """One ledger-backed tool action shared by every Agent loop.

    This class deliberately contains no XAE or permission policy. The caller
    decides whether an action is allowed and supplies the actual executor; the
    lifecycle and replay/uncertain semantics remain identical for foreground
    turns and compatibility workers.
    """

    def __init__(self, store: "ConversationStore", request: ActionRequest) -> None:
        self.store = store
        self.request = request
        self.payload = request.envelope()
        self.reservation = store.begin_tool_execution(
            run_id=request.run_id,
            step_id=request.step_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            args=request.arguments,
            category=request.category,
            danger=request.danger,
            readonly=request.readonly,
            target_pid=request.target_pid,
            idempotency_key=self.payload["idempotency_key"],
        )
        self.execution = self.reservation["execution"]
        self.execution_id = str(self.execution.get("id") or "")

    @property
    def disposition(self) -> str:
        return str(self.reservation["disposition"])

    def replay_result(self) -> tuple[Any, bool]:
        if self.disposition == "replay":
            return self.execution.get("result"), self.execution.get("status") == "completed"
        if self.disposition == "uncertain":
            return {
                "error": "该工具调用在上次中断时结果未知，已阻止自动重复执行",
                "execution_id": self.execution_id,
                "status": self.execution.get("status"),
            }, False
        raise RuntimeError("A newly reserved action has no replay result")

    def finish(self, result: Any, *, ok: bool) -> Any:
        error = ""
        if not ok and isinstance(result, dict):
            error = str(result.get("error") or result.get("denied") or "")
        uncertain = isinstance(result, dict) and (
            result.get("uncertain") is True or result.get("status") == "uncertain")
        self.execution = self.store.finish_tool_execution(
            self.execution_id, "uncertain" if uncertain else ("completed" if ok else "failed"),
            result=result, error=error,
        )
        return result

    def deny(self, reason: str, result: dict | None = None) -> dict:
        result = dict(result or {})
        result.setdefault("denied", str(reason or "已拒绝"))
        result.setdefault("status", "denied")
        result.setdefault("written", False)
        result.setdefault("not_executed", True)
        result.setdefault("authorization_blocked", True)
        self.execution = self.store.finish_tool_execution(
            self.execution_id, "denied", result=result, error=str(reason or "已拒绝")
        )
        return result

    def uncertain(self, reason: str) -> dict:
        result = {"cancelled": True, "uncertain": True}
        self.execution = self.store.finish_tool_execution(
            self.execution_id, "uncertain", result=result, error=str(reason or "")
        )
        return result


def execution_failure_report(store, run_id: str, reason: str) -> str:
    """Render durable mutation facts even when an approval aborts a turn."""
    if store is None or not run_id:
        return reason
    rows = store.agent_run_snapshot(run_id).get("tool_executions", [])
    rows = [row for row in rows if not row.get("readonly")]
    if not rows:
        return reason
    labels = {"completed": "已执行（不等于功能验证通过）", "denied": "未执行",
              "failed": "失败，是否产生部分影响须核对",
              "running": "执行结果未知", "uncertain": "执行结果未知"}
    lines = ["本轮已中止：" + reason, "执行台账："]
    for row in rows:
        result = row.get("result") or {}
        label = labels.get(row.get("status"), "状态待核对")
        if isinstance(result, dict) and result.get("not_executed") is True:
            label = "未执行"
        elif (isinstance(result, dict) and result.get('written') is False
              and result.get('failure_stage') == 'pre_write_review'):
            label = '写前检查未通过，未写入'
        args = row.get("args") or {}
        scope = {key: args[key] for key in ("name", "runtime", "action") if key in args}
        if row.get("tool_name") == "plc_write_value" and isinstance(args.get("value"), (bool, int, float)):
            scope["value"] = args["value"]
        lines.append(f"- {row.get('tool_name')}: {label}；对象 {canonical_json(scope)}")
    lines.append("已执行动作不会因后续拒绝而撤销；恢复未获执行证据时不得称已恢复。请先只读核对实际状态，恢复或重试需要独立授权。")
    return "\n".join(lines)


class DurableRun:
    """Shared Run/Model-Step lifecycle for foreground and worker loops."""

    def __init__(
        self, store: "ConversationStore", thread_id: str, request_text: str, *,
        provider_model: str = "", target_pid: int = 0, solution: str = "",
    ) -> None:
        self.store = store
        self.run_id = store.start_agent_run(
            thread_id, request_text, provider_model=provider_model,
            target_pid=target_pid, solution=solution,
        )
        self.model_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.current_step_id = ""
        self.finished = False

    def start_model_step(self, input_payload=None) -> str:
        if self.finished:
            raise RuntimeError("Cannot start a step on a finished Agent run")
        if self.current_step_id:
            raise RuntimeError("The previous model step is still running")
        self.current_step_id = self.store.start_agent_step(
            self.run_id, self.model_calls + 1, "model", input_payload,
        )
        return self.current_step_id

    def complete_model_step(self, output_payload: dict) -> str:
        if not self.current_step_id:
            raise RuntimeError("No running model step to complete")
        completed = self.current_step_id
        self.store.finish_agent_step(
            completed, "completed", output_payload=output_payload,
        )
        usage = output_payload.get("usage") or {}
        self.model_calls += 1
        self.tokens_in += int(usage.get("in") or 0)
        self.tokens_out += int(usage.get("out") or 0)
        self.current_step_id = ""
        return completed

    def fail_model_step(self, status: str, error: str) -> None:
        if not self.current_step_id:
            return
        self.store.finish_agent_step(
            self.current_step_id, status, error=str(error or "")
        )
        self.current_step_id = ""

    def finish(self, status: str, error: str = "") -> None:
        if self.finished:
            return
        self.store.finish_agent_run(
            self.run_id, status, model_calls=self.model_calls,
            tokens_in=self.tokens_in, tokens_out=self.tokens_out, error=error,
        )
        self.finished = True
