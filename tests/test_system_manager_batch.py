from __future__ import annotations

from tc_agent.system_manager_batch import BatchExecutor, BatchItem


class BatchTransport:
    def __init__(self):
        self.batch_calls = []
        self.single_calls = []

    def batch_request(self, requests, *, timeout_s):
        self.batch_calls.append((requests, timeout_s))
        return [{"id": request["id"], "ok": request["id"] != "bad"} for request in requests]

    def request(self, command, payload, *, timeout_s):
        self.single_calls.append((command, payload, timeout_s))
        return {"ok": True, "revision": "after"}


def test_read_batch_combines_transport_call_and_keeps_partial_status():
    transport = BatchTransport()
    result = BatchExecutor(transport).execute([
        BatchItem("one", "sm.plcpou", {"tid": 1}),
        BatchItem("bad", "sm.plccompilermsg", {"tid": 1}),
    ])
    assert result.as_dict()["status"] == "partial"
    assert [item.status for item in result.items] == ["succeeded", "failed"]
    assert len(transport.batch_calls) == 1
    assert transport.single_calls == []


def test_write_requires_permission_preflight_revision_and_readback():
    transport = BatchTransport()
    write = BatchItem("write", "sm.plcpou", {"tname": "MAIN"}, "write", expected_revision="r1")
    blocked = BatchExecutor(transport).execute([write])
    assert blocked.items[0].status == "blocked"

    conflict = BatchExecutor(transport).execute(
        [write], allow_mutation=True, preflight=lambda item: {"revision": "r2"},
    )
    assert conflict.items[0].status == "conflict"

    success = BatchExecutor(transport).execute(
        [write], allow_mutation=True,
        preflight=lambda item: {"revision": "r1"},
        readback=lambda item, reply: reply["revision"] == "after",
    )
    assert success.items[0].status == "succeeded"
    assert success.items[0].verified is True


def test_write_without_post_readback_is_blocked_even_after_preflight():
    transport = BatchTransport()
    write = BatchItem("write", "sm.plcpou", {"tname": "MAIN"}, "write", expected_revision="r1")
    result = BatchExecutor(transport).execute(
        [write], allow_mutation=True, preflight=lambda item: {"revision": "r1"},
    )
    assert result.items[0].status == "blocked"
    assert "readback" in result.items[0].error
    assert transport.single_calls == []


def test_cancel_marks_unstarted_items_without_claiming_transport_cancellation():
    transport = BatchTransport()
    result = BatchExecutor(transport).execute(
        [BatchItem("one", "sm.plcpou")], cancel=lambda: True,
    )
    assert result.items[0].status == "cancelled"
    assert transport.batch_calls == []
    assert transport.single_calls == []
