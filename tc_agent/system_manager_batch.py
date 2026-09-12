"""Bounded batch execution for direct System Manager adapters.

Reads may be sent through one transport batch.  Mutations are opt-in, require
an explicit permission confirmation and an expected revision/hash, and are
read back when a verifier is supplied.  A timeout is reported as uncertain if
the transport has no cancellation primitive; it never claims that an in-flight
request was cancelled.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class BatchItem:
    item_id: str
    command: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    mode: str = "read"
    expected_revision: str | int | None = None


@dataclass
class BatchItemResult:
    item_id: str
    status: str
    response: Any = None
    error: str = ""
    verified: bool | None = None
    uncertain: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "status": self.status,
            "response": self.response,
            "error": self.error,
            "verified": self.verified,
            "uncertain": self.uncertain,
        }


@dataclass
class BatchResult:
    items: list[BatchItemResult]
    elapsed_ms: float
    transport_batch_calls: int = 0
    transport_single_calls: int = 0

    @property
    def succeeded(self) -> bool:
        return bool(self.items) and all(item.status == "succeeded" for item in self.items)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "status": "succeeded" if self.succeeded else "partial" if any(
                item.status == "succeeded" for item in self.items
            ) else "failed",
            "counts": counts,
            "items": [item.as_dict() for item in self.items],
            "elapsed_ms": self.elapsed_ms,
            "transport_batch_calls": self.transport_batch_calls,
            "transport_single_calls": self.transport_single_calls,
        }


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    return bool(cancel.is_set()) if hasattr(cancel, "is_set") else bool(cancel)


def _reply_error(reply: Any) -> str:
    if not isinstance(reply, Mapping):
        return "reply is not an object"
    value = reply.get("protocolError", reply.get("protocol_error"))
    if value is not None:
        return f"protocol error: {value}"
    if reply.get("ok") is False or reply.get("success") is False:
        return str(reply.get("error") or reply.get("message") or "request failed")
    return ""


class BatchExecutor:
    """Execute direct-interface operations while keeping per-item evidence."""

    def __init__(self, transport: Any, *, lock_factory: Callable[[Any], Any] | None = None,
                 max_items: int = 100):
        self.transport = transport
        self.lock_factory = lock_factory
        self.max_items = max(1, int(max_items))

    def _request_one(self, item: BatchItem, deadline: float) -> Any:
        if time.monotonic() >= deadline:
            raise TimeoutError("batch deadline reached before request")
        request = getattr(self.transport, "request", None)
        if not callable(request):
            raise TypeError("transport does not provide request")
        return request(item.command, dict(item.payload), timeout_s=max(0.001, deadline - time.monotonic()))

    def _request_many(self, items: Sequence[BatchItem], deadline: float) -> Any:
        if time.monotonic() >= deadline:
            raise TimeoutError("batch deadline reached before request")
        request = getattr(self.transport, "batch_request", None)
        if not callable(request):
            return None
        return request(
            [{"id": item.item_id, "cmd": item.command, **dict(item.payload)} for item in items],
            timeout_s=max(0.001, deadline - time.monotonic()),
        )

    @staticmethod
    def _batch_replies(raw: Any, items: Sequence[BatchItem]) -> dict[str, Any]:
        if isinstance(raw, Mapping) and isinstance(raw.get("items"), list):
            raw = raw["items"]
        if isinstance(raw, list):
            keyed = {
                str(value.get("id", value.get("item_id"))): value
                for value in raw if isinstance(value, Mapping)
                and value.get("id", value.get("item_id")) is not None
            }
            if keyed:
                return keyed
            return {item.item_id: value for item, value in zip(items, raw)}
        if len(items) == 1:
            return {items[0].item_id: raw}
        return {}

    def execute(self, items: Sequence[BatchItem], *, allow_mutation: bool = False,
                preflight: Callable[[BatchItem], Any] | None = None,
                readback: Callable[[BatchItem, Any], Any] | None = None,
                cancel: Any = None, timeout_s: float = 30.0, scope: Any = None) -> BatchResult:
        started = time.monotonic()
        entries = list(items)
        if not entries:
            raise ValueError("batch must contain at least one item")
        if len(entries) > self.max_items:
            raise ValueError(f"batch is limited to {self.max_items} items")
        if len({item.item_id for item in entries}) != len(entries):
            raise ValueError("batch item_id values must be unique")
        if any(item.mode not in {"read", "write"} for item in entries):
            raise ValueError("batch item mode must be read or write")
        deadline = started + max(0.001, float(timeout_s))
        results: dict[str, BatchItemResult] = {}
        batch_calls = single_calls = 0
        guard = self.lock_factory(scope) if self.lock_factory else nullcontext()
        with guard:
            reads = [item for item in entries if item.mode == "read"]
            batch_request = getattr(self.transport, "batch_request", None)
            if reads and callable(batch_request) and not _cancelled(cancel):
                try:
                    raw = self._request_many(reads, deadline)
                    batch_calls += 1
                    replies = self._batch_replies(raw, reads)
                    for item in reads:
                        reply = replies.get(item.item_id)
                        error = _reply_error(reply)
                        results[item.item_id] = BatchItemResult(
                            item.item_id, "failed" if error else "succeeded", reply, error,
                            verified=False if error else True,
                            uncertain=time.monotonic() > deadline,
                        )
                except TimeoutError as exc:
                    for item in reads:
                        results[item.item_id] = BatchItemResult(item.item_id, "timeout", error=str(exc), uncertain=True)
                except Exception as exc:
                    for item in reads:
                        results[item.item_id] = BatchItemResult(item.item_id, "failed", error=str(exc))
            else:
                for item in reads:
                    if _cancelled(cancel):
                        results[item.item_id] = BatchItemResult(item.item_id, "cancelled")
                        continue
                    try:
                        reply = self._request_one(item, deadline)
                        single_calls += 1
                        error = _reply_error(reply)
                        results[item.item_id] = BatchItemResult(
                            item.item_id, "failed" if error else "succeeded", reply, error,
                            verified=False if error else True,
                            uncertain=time.monotonic() > deadline,
                        )
                    except TimeoutError as exc:
                        results[item.item_id] = BatchItemResult(item.item_id, "timeout", error=str(exc), uncertain=True)
                    except Exception as exc:
                        results[item.item_id] = BatchItemResult(item.item_id, "failed", error=str(exc))

            for item in entries:
                if item.mode != "write" or item.item_id in results:
                    continue
                if _cancelled(cancel):
                    results[item.item_id] = BatchItemResult(item.item_id, "cancelled")
                    continue
                if not allow_mutation:
                    results[item.item_id] = BatchItemResult(item.item_id, "blocked", error="mutation permission not granted")
                    continue
                if item.expected_revision is None:
                    results[item.item_id] = BatchItemResult(item.item_id, "blocked", error="write requires expected_revision")
                    continue
                if preflight is None:
                    results[item.item_id] = BatchItemResult(item.item_id, "blocked", error="write requires preflight verifier")
                    continue
                try:
                    current = preflight(item)
                    current_revision = current.get("revision") if isinstance(current, Mapping) else None
                    if current_revision != item.expected_revision:
                        results[item.item_id] = BatchItemResult(
                            item.item_id, "conflict", response=current,
                            error="expected_revision does not match current source",
                        )
                        continue
                    if readback is None:
                        results[item.item_id] = BatchItemResult(item.item_id, "blocked", error="write requires post-write readback verifier")
                        continue
                    reply = self._request_one(item, deadline)
                    single_calls += 1
                    error = _reply_error(reply)
                    if error:
                        results[item.item_id] = BatchItemResult(item.item_id, "failed", reply, error, verified=False)
                        continue
                    verified: bool | None = bool(readback(item, reply))
                    if not verified:
                        results[item.item_id] = BatchItemResult(
                            item.item_id, "failed", reply, "post-write readback mismatch", verified=False,
                        )
                        continue
                    results[item.item_id] = BatchItemResult(item.item_id, "succeeded", reply, verified=verified)
                except TimeoutError as exc:
                    results[item.item_id] = BatchItemResult(item.item_id, "timeout", error=str(exc), uncertain=True)
                except Exception as exc:
                    results[item.item_id] = BatchItemResult(item.item_id, "failed", error=str(exc))
        ordered = [results.get(item.item_id, BatchItemResult(item.item_id, "cancelled")) for item in entries]
        return BatchResult(ordered, round((time.monotonic() - started) * 1000, 3), batch_calls, single_calls)
