"""Exercise the actual nested approval coroutine without XAE or live sockets."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tc_agent import backend


@pytest.mark.parametrize("decision", ["cancel", "allow", "deny"])
def test_per_call_approval_outcomes(decision):
    tree = ast.parse(Path(backend.__file__).read_text(encoding="utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "approve")
    class Globals(ast.NodeTransformer):
        def visit_Nonlocal(self, item):
            return ast.copy_location(ast.Global(names=item.names), item)
    node = Globals().visit(node)
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), node], type_ignores=[]))
    events = []
    async def publish(**event):
        events.append(event)
    ns = dict(vars(backend), perm_seq=0, active_thread_id="main", conversation_store=None,
              publish_live=publish, pending={}, FOREGROUND_PERMISSIONS={},
              approved_plan_for_action=[""], _tool_danger=lambda _: "",
              CURRENT_FOREGROUND_TURN=SimpleNamespace(get=lambda: None))
    exec(compile(module, "approve", "exec"), ns)
    outcome = {}
    async def run():
        task = asyncio.create_task(ns["approve"]("plc_write", {"name": "MAIN"}, outcome=outcome))
        await asyncio.sleep(0)
        assert not task.done()
        assert len(ns['pending']) == 1
        # No time-based wrapper or approval-expiry branch is allowed.
        assert not any(isinstance(n,ast.Attribute) and n.attr=='wait_for' for n in ast.walk(node))
        if decision == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return False
        next(iter(ns['pending'].values())).set_result({'allow':decision=='allow'})
        return await task
    assert asyncio.run(run()) is (decision == "allow")
    assert not ns["pending"] and not ns["FOREGROUND_PERMISSIONS"]
    assert events[0]["authorization_plan"] is None
    assert ns["approved_plan_for_action"] == [""]
    assert outcome == {}


def test_model_catalog_does_not_offer_cross_turn_plan():
    assert all(t["name"] != "tc_request_authorization" for t in backend._tool_schema())


def test_expiry_is_not_an_execution_failure_or_gate_rejection():
    from tc_agent.execution_policy import gate_rejected
    result = {"status": "approval_expired", "not_executed": True, "authorization_blocked": False}
    assert not backend._actual_tool_failure(result, False)
    assert not gate_rejected(result)
