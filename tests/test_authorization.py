from pathlib import Path

import pytest

from tc_agent.authorization import (
    AuthorizationError,
    confirmable,
    context_matches,
    explicit_report_actions,
    expand_actions,
    make_plan,
    plan_allows_action,
)
from tc_agent.conversation_store import ConversationStore


def _plan(thread_id="main", **kwargs):
    return make_plan(
        thread_id=thread_id, solution=r"G:\project\Demo.sln", pid=42,
        process_identity=(42, 100, 200), target_netid="1.2.3.4.1.1",
        runtimes=[{"name": "Untitled1", "ads_port": 851}],
        actions=[{"name": "tc_online", "args": {"runtime": "Untitled1"}}],
        backend_session_id="session-a", **kwargs,
    )


def test_confirmation_is_exact_and_composite_is_expanded():
    assert confirmable(" 授权 ")
    assert confirmable("confirm")
    assert not confirmable("授权执行全部部署")
    actions = expand_actions("tc_online", {"runtime": "Untitled1"})
    assert [item["name"] for item in actions] == ["tc_login", "tc_start"]
    assert all(item["args"] == {"runtime": "Untitled1"} for item in actions)


def test_context_requires_exact_identity_target_and_endpoints():
    plan = _plan()
    current = plan["context"].copy()
    assert context_matches(plan, current)[0]
    for field, value in (("pid", 43), ("process_identity", [42, 100, 201]),
                         ("target_netid", "other"),
                         ("runtimes", [{"name": "Untitled1", "ads_port": 852}]),
                         ("backend_session_id", "session-b")):
        changed = dict(current)
        changed[field] = value
        assert not context_matches(plan, changed)[0]


def test_report_parser_is_narrow_and_does_not_parse_history_style_text():
    actions = explicit_report_actions(
        "要完成在线验证，需要确认：Activate Configuration；Restart；Login+Start。"
    )
    assert [item["name"] for item in actions] == [
        "tc_activate", "tc_restart", "tc_login", "tc_start"
    ]
    assert explicit_report_actions("历史中曾经问过是否授权重启") == []
    assert explicit_report_actions("是否需要重启？") == []


def test_store_persists_one_plan_and_consumes_confirmation_once(tmp_path: Path):
    store = ConversationStore(tmp_path / "agent.db")
    thread_id = store.list_threads()[0]["id"]
    plan = _plan(thread_id)
    store.create_authorization_plan(plan)
    with pytest.raises(AuthorizationError):
        store.create_authorization_plan(_plan(thread_id))
    pending = store.pending_authorization_plan(thread_id)
    assert pending["id"] == plan["id"]
    authorized = store.confirm_authorization_plan(plan["id"])
    assert authorized["status"] == "authorized"
    assert plan_allows_action(authorized, "tc_login", {"runtime": "Untitled1"})
    with pytest.raises(AuthorizationError):
        store.confirm_authorization_plan(plan["id"])
    completed = store.record_authorization_action(
        plan["id"], "tc_online", {"runtime": "Untitled1"}, outcome="completed"
    )
    assert completed["status"] == "completed"
    with pytest.raises(AuthorizationError):
        store.record_authorization_action(
            plan["id"], "tc_online", {"runtime": "Untitled1"}, outcome="completed"
        )


def test_partial_approval_is_exact_subset(tmp_path: Path):
    store = ConversationStore(tmp_path / "agent.db")
    thread_id = store.list_threads()[0]["id"]
    plan = make_plan(
        thread_id=thread_id, solution="G:/project/Demo.sln", pid=42,
        process_identity=(42, 1), target_netid="net", runtimes=[],
        actions=[{"name": "tc_activate"}, {"name": "tc_restart"}],
        backend_session_id="session-a",
    )
    store.create_authorization_plan(plan)
    selected = [plan["actions"][0]["key"]]
    authorized = store.confirm_authorization_plan(plan["id"], selection=selected)
    assert [item["name"] for item in authorized["approved_actions"]] == ["tc_activate"]
    assert plan_allows_action(authorized, "tc_activate")
    assert not plan_allows_action(authorized, "tc_restart")


def test_expired_plan_cannot_be_confirmed(tmp_path: Path):
    store = ConversationStore(tmp_path / "agent.db")
    thread_id = store.list_threads()[0]["id"]
    plan = _plan(thread_id, ttl=1)
    store.create_authorization_plan(plan)
    with pytest.raises(AuthorizationError):
        store.confirm_authorization_plan(plan["id"], now=plan["expires_at"] + 1)
    assert store.get_authorization_plan(plan["id"])["status"] == "expired"

