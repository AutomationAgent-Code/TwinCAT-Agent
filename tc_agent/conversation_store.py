"""Project-local SQLite persistence for TwinCAT Agent conversations.

The provider-neutral database stores thread metadata, model messages, UI
events, compressed summaries, interruption recovery, project memories and the
inter-thread mailbox.  Legacy worker/audit tables remain readable for database
compatibility; current conversation storage does not depend on JSON history.
"""

from __future__ import annotations

import json
import base64
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath


SCHEMA_VERSION = 8
RUNTIME_SESSION_ID = uuid.uuid4().hex
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?key|secret|token|password|authorization)\s*[:=]|"
    r"\b(sk-[A-Za-z0-9_-]{12,}|TCAG1\.[A-Za-z0-9._-]+)\b"
)
_active_store: ContextVar["ConversationStore | None"] = ContextVar(
    "tc_agent_conversation_store", default=None
)
_active_thread: ContextVar[str] = ContextVar("tc_agent_conversation_thread", default="")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class ConversationStore:
    def __init__(self, db_path: Path, legacy_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._recover_interrupted_threads()
        if legacy_path:
            self._migrate_legacy(Path(legacy_path))
        # A database may contain only archived conversations (for example after
        # an older client or interrupted multi-panel race).  The active list
        # must never be empty because the WebView select and backend history
        # binding both require a valid current conversation.
        if not self.list_threads():
            self.create_thread("主对话", kind="main")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    data BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'chat',
                    parent_id TEXT,
                    status TEXT NOT NULL DEFAULT 'idle',
                    archived INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    working_directory TEXT NOT NULL DEFAULT '.',
                    tool_categories TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(parent_id) REFERENCES threads(id)
                );
                CREATE TABLE IF NOT EXISTS thread_state (
                    thread_id TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    events_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    recovery_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS mailbox (
                    id TEXT PRIMARY KEY,
                    from_thread TEXT NOT NULL,
                    to_thread TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    read_at REAL,
                    FOREIGN KEY(from_thread) REFERENCES threads(id),
                    FOREIGN KEY(to_thread) REFERENCES threads(id)
                );
                CREATE INDEX IF NOT EXISTS idx_mailbox_to_created
                    ON mailbox(to_thread, created_at);
                CREATE TABLE IF NOT EXISTS change_proposals (
                    id TEXT PRIMARY KEY,
                    worker_thread TEXT NOT NULL,
                    parent_thread TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    FOREIGN KEY(worker_thread) REFERENCES threads(id),
                    FOREIGN KEY(parent_thread) REFERENCES threads(id)
                );
                CREATE INDEX IF NOT EXISTS idx_proposals_parent_status
                    ON change_proposals(parent_thread, status, created_at);
                CREATE TABLE IF NOT EXISTS worker_runs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    provider_model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    tokens_in INTEGER NOT NULL DEFAULT 0,
                    tokens_out INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_runs_thread_started
                    ON worker_runs(thread_id, started_at);
                CREATE TABLE IF NOT EXISTS project_memories (
                    id TEXT PRIMARY KEY,
                    memory_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_thread TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(source_thread) REFERENCES threads(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_key
                    ON project_memories(memory_key) WHERE active=1;
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    request_text TEXT NOT NULL DEFAULT '',
                    provider_model TEXT NOT NULL DEFAULT '',
                    target_pid INTEGER NOT NULL DEFAULT 0,
                    solution TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    current_step INTEGER NOT NULL DEFAULT 0,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    tokens_in INTEGER NOT NULL DEFAULT 0,
                    tokens_out INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_thread_started
                    ON agent_runs(thread_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status
                    ON agent_runs(status, updated_at);
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, sequence, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_steps_run_sequence
                    ON agent_steps(run_id, sequence);
                CREATE TABLE IF NOT EXISTS tool_executions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '{}',
                    category TEXT NOT NULL DEFAULT '',
                    danger TEXT NOT NULL DEFAULT '',
                    readonly INTEGER NOT NULL DEFAULT 0,
                    target_pid INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    result_json TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(step_id) REFERENCES agent_steps(id) ON DELETE SET NULL,
                    UNIQUE(run_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_tool_executions_run_started
                    ON tool_executions(run_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_tool_executions_status
                    ON tool_executions(status, started_at);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool_execution_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    decision_json TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(tool_execution_id) REFERENCES tool_executions(id)
                        ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_run_created
                    ON approvals(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_approvals_status
                    ON approvals(status, created_at);
                CREATE TABLE IF NOT EXISTS authorization_plans (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    plan_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    confirmed_at REAL,
                    invalidated_at REAL,
                    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_authorization_one_pending
                    ON authorization_plans(thread_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_authorization_thread_status
                    ON authorization_plans(thread_id, status, created_at);
                """
            )
            # Existing project databases predate conversation pinning. Keep
            # the migration additive so no history is rewritten or lost.
            thread_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(threads)")
            }
            if "pinned" not in thread_columns:
                db.execute(
                    "ALTER TABLE threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
                )
            if "working_directory" not in thread_columns:
                db.execute(
                    "ALTER TABLE threads ADD COLUMN working_directory TEXT NOT NULL DEFAULT '.'"
                )
            if "tool_categories" not in thread_columns:
                db.execute(
                    "ALTER TABLE threads ADD COLUMN tool_categories TEXT NOT NULL DEFAULT '[]'"
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def _migrate_legacy(self, path: Path) -> None:
        if not path.is_file():
            return
        with self._connect() as db:
            done = db.execute(
                "SELECT value FROM metadata WHERE key='legacy_migrated'"
            ).fetchone()
            if done:
                return
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return
            now = time.time()
            thread_id = "main"
            db.execute(
                "INSERT OR IGNORE INTO threads(id,title,kind,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (thread_id, "主对话", "main", "idle", now, now),
            )
            db.execute(
                "INSERT OR REPLACE INTO thread_state"
                "(thread_id,messages_json,events_json,summary,recovery_json) VALUES(?,?,?,?,?)",
                (
                    thread_id,
                    _json(data.get("messages") or []),
                    _json(data.get("events") or []),
                    str(data.get("summary") or ""),
                    _json(data.get("recovery") or {}),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('legacy_migrated',?)",
                (str(now),),
            )

    def _recover_interrupted_threads(self) -> None:
        """A process restart cannot leave workers falsely shown as running."""
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key='runtime_session'"
            ).fetchone()
            if row and row["value"] == RUNTIME_SESSION_ID:
                return
            db.execute(
                "UPDATE threads SET status='interrupted',updated_at=? "
                "WHERE status IN ('queued','running')",
                (time.time(),),
            )
            db.execute(
                "UPDATE worker_runs SET status='interrupted',finished_at=?,"
                "error=CASE WHEN error='' THEN '后端服务重启，任务可从断点恢复' ELSE error END "
                "WHERE status='running' AND finished_at IS NULL",
                (time.time(),),
            )
            now = time.time()
            db.execute(
                "UPDATE agent_runs SET status='interrupted',updated_at=?,finished_at=?,"
                "error=CASE WHEN error='' THEN '后端服务重启，执行已中断' ELSE error END "
                "WHERE status='running' AND finished_at IS NULL",
                (now, now),
            )
            # A mutating action may have reached XAE before the backend died.
            # Mark it uncertain instead of silently retrying and risking a
            # duplicate write. Completed actions retain their replayable result.
            db.execute(
                "UPDATE tool_executions SET status='uncertain',finished_at=?,"
                "error=CASE WHEN error='' THEN 'Agent 重启时工具仍在执行；结果未知，禁止自动重放' "
                "ELSE error END WHERE status='running' AND finished_at IS NULL",
                (now,),
            )
            db.execute(
                "UPDATE agent_steps SET status='interrupted',finished_at=?,"
                "error=CASE WHEN error='' THEN 'Agent 重启时步骤未完成' ELSE error END "
                "WHERE status='running' AND finished_at IS NULL",
                (now,),
            )
            db.execute(
                "UPDATE approvals SET status='expired',resolved_at=?,"
                "decision_json=COALESCE(decision_json, ?) "
                "WHERE status='pending' AND resolved_at IS NULL",
                (now, _json({"allow": False, "reason": "Agent 已重启"})),
            )
            db.execute(
                "UPDATE authorization_plans SET status='invalidated',invalidated_at=? "
                "WHERE status IN ('pending','authorized')",
                (now,),
            )
            self._reconcile_interrupted_tool_history(db)
            db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('runtime_session',?)",
                (RUNTIME_SESSION_ID,),
            )

    @staticmethod
    def _reconcile_interrupted_tool_history(db) -> None:
        """Repair orphan model tool calls from durable execution outcomes.

        A process can stop after an assistant tool request was saved but before
        its matching model-context result was appended. Strict providers reject
        that history. More importantly, blindly asking the model again could
        duplicate a PLC mutation. The ledger is authoritative during repair.
        """
        states = db.execute(
            "SELECT ts.thread_id,ts.messages_json FROM thread_state ts "
            "WHERE EXISTS (SELECT 1 FROM agent_runs ar "
            "WHERE ar.thread_id=ts.thread_id AND ar.status='interrupted')"
        ).fetchall()
        for state in states:
            try:
                messages = json.loads(state["messages_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            repaired: list[dict] = []
            changed = False
            index = 0
            while index < len(messages):
                message = messages[index]
                repaired.append(message)
                calls = (
                    message.get("tool_calls") or []
                    if message.get("role") == "assistant" else []
                )
                if not calls:
                    index += 1
                    continue
                next_index = index + 1
                existing: dict[str, dict] = {}
                while (
                    next_index < len(messages)
                    and messages[next_index].get("role") == "tool"
                ):
                    tool_message = messages[next_index]
                    existing[str(tool_message.get("id") or "")] = tool_message
                    next_index += 1
                for call in calls:
                    call_id = str(call.get("id") or "")
                    if call_id in existing:
                        repaired.append(existing[call_id])
                        continue
                    execution = db.execute(
                        "SELECT te.* FROM tool_executions te "
                        "JOIN agent_runs ar ON ar.id=te.run_id "
                        "WHERE ar.thread_id=? AND te.tool_call_id=? "
                        "ORDER BY te.started_at DESC LIMIT 1",
                        (state["thread_id"], call_id),
                    ).fetchone()
                    if execution and execution["status"] in {
                        "completed", "failed", "denied"
                    }:
                        raw = execution["result_json"]
                        result = json.loads(raw) if raw else {
                            "error": execution["error"] or "工具未返回结果"
                        }
                    elif execution:
                        result = {
                            "error": "Agent 中断时该工具的执行结果未知，已禁止自动重放",
                            "execution_id": execution["id"],
                            "status": execution["status"],
                        }
                    else:
                        result = {"aborted": "该工具调用未开始执行，已在恢复时跳过"}
                    repaired.append({
                        "role": "tool", "id": call_id,
                        "name": call.get("name", ""), "result": result,
                    })
                    changed = True
                index = next_index
            if changed:
                db.execute(
                    "UPDATE thread_state SET messages_json=? WHERE thread_id=?",
                    (_json(repaired), state["thread_id"]),
                )

    def create_thread(
        self, title: str = "新对话", *, kind: str = "chat", parent_id: str | None = None
    ) -> dict:
        thread_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO threads(id,title,kind,parent_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (thread_id, (title or "新对话")[:120], kind, parent_id, "idle", now, now),
            )
            db.execute("INSERT INTO thread_state(thread_id) VALUES(?)", (thread_id,))
        return self.get_thread(thread_id)

    def rename_thread(self, thread_id: str, title: str) -> dict:
        clean = str(title or "").strip()
        if not clean:
            raise ValueError("Conversation title cannot be empty")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET title=?,updated_at=? WHERE id=? AND archived=0",
                (clean[:120], time.time(), thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Conversation not found: {thread_id}")
        return self.get_thread(thread_id)

    def copy_creation_context(self, thread_id: str, destination):
        """Copy this creation conversation and its referenced images, retaining source."""
        state = self.load_state(thread_id)
        original = self.get_thread(thread_id)
        image_ids = {a.get('attachment_id') for e in state['events']
                     for a in e.get('attachments', []) if a.get('attachment_id')}
        with self._connect() as source:
            images = [source.execute('SELECT * FROM attachments WHERE id=?', (i,)).fetchone()
                      for i in image_ids]
        with destination._connect() as target:
            for image in images:
                if image is not None:
                    existing = target.execute('SELECT sha256 FROM attachments WHERE id=?', (image['id'],)).fetchone()
                    if existing and existing['sha256'] != image['sha256']:
                        raise ValueError('Attachment ID collision during project handoff')
                    target.execute('INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?)', tuple(image))
        thread = destination.create_thread(original.get('title') or '创建工程')
        destination.save_state(thread['id'], state)
        destination.set_status(thread['id'], original.get('status') or 'idle')
        return thread

    def set_pinned(self, thread_id: str, pinned: bool) -> dict:
        """Pin or unpin an active conversation without changing its recency."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET pinned=? WHERE id=? AND archived=0",
                (1 if pinned else 0, thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Conversation not found: {thread_id}")
        return self.get_thread(thread_id)

    def set_working_directory(self, thread_id: str, path: str) -> dict:
        """Set the conversation's selected XAE tree/context path."""
        raw = str(path or ".").strip().replace("\\", "/") or "."
        windows_path = PureWindowsPath(raw)
        posix_path = PurePosixPath(raw)
        if windows_path.is_absolute() or posix_path.is_absolute() or windows_path.drive:
            raise ValueError("工程节点必须是当前 XAE 树中的相对上下文路径")
        if ".." in posix_path.parts:
            raise ValueError("工程节点不能使用 .. 越出当前项目")
        clean = "/".join(part for part in posix_path.parts if part not in {"", "."}) or "."
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET working_directory=? WHERE id=? AND archived=0",
                (clean, thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Conversation not found: {thread_id}")
        return self.get_thread(thread_id)

    def set_tool_categories(self, thread_id: str, categories: list[str] | None) -> dict:
        """Assign tool categories to one conversation; empty means all categories."""
        values = []
        for item in categories or []:
            value = str(item or "").strip()
            if value and value not in values:
                values.append(value)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET tool_categories=?,updated_at=? WHERE id=? AND archived=0",
                (_json(values), time.time(), thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Conversation not found: {thread_id}")
        return self.get_thread(thread_id)

    @staticmethod
    def parse_tool_categories(thread: dict | None) -> list[str]:
        if not thread:
            return []
        raw = thread.get("tool_categories") or "[]"
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            values = []
        return [str(item) for item in values or [] if str(item).strip()]

    def fork_thread(self, source_id: str, title: str = "") -> dict:
        source = self.get_thread(source_id)
        state = self.load_state(source_id)
        forked = self.create_thread(
            title or f"{source['title']} · 分叉", kind="chat",
            parent_id=source.get("parent_id"),
        )
        # Keep semantic/model context, but not UI replay or interruption state.
        self.save_state(forked["id"], {
            "messages": state["messages"], "events": [],
            "summary": state["summary"], "recovery": {},
        })
        self.set_working_directory(
            forked["id"], str(source.get("working_directory") or ".")
        )
        self.set_tool_categories(
            forked["id"], self.parse_tool_categories(source)
        )
        return self.get_thread(forked["id"])

    def reparent_thread(self, thread_id: str, new_parent_id: str) -> dict:
        thread = self.get_thread(thread_id)
        parent = self.get_thread(new_parent_id)
        if thread_id == new_parent_id:
            raise ValueError("Conversation cannot be its own parent")
        cursor = parent
        visited = set()
        while cursor.get("parent_id"):
            if cursor["id"] in visited or cursor["parent_id"] == thread_id:
                raise ValueError("Conversation handoff would create a cycle")
            visited.add(cursor["id"])
            cursor = self.get_thread(cursor["parent_id"])
        with self._connect() as db:
            db.execute(
                "UPDATE threads SET parent_id=?,updated_at=? WHERE id=?",
                (new_parent_id, time.time(), thread_id),
            )
        return self.get_thread(thread_id)

    def get_thread(self, thread_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            raise KeyError(f"Conversation not found: {thread_id}")
        return dict(row)

    def list_threads(self, *, include_archived: bool = False) -> list[dict]:
        query = (
            "SELECT threads.*, (SELECT COUNT(*) FROM mailbox "
            "WHERE mailbox.to_thread=threads.id AND mailbox.read_at IS NULL) "
            "AS unread_count FROM threads"
        )
        if not include_archived:
            query += " WHERE archived=0"
        query += " ORDER BY pinned DESC, updated_at DESC, created_at ASC"
        with self._connect() as db:
            return [dict(row) for row in db.execute(query).fetchall()]

    def archive_thread(self, thread_id: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET archived=1,status='archived',updated_at=? WHERE id=? "
                "AND status NOT IN ('queued','running')",
                (time.time(), thread_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Conversation is running or does not exist")

    def set_status(self, thread_id: str, status: str) -> None:
        allowed = {"idle", "queued", "running", "completed", "incomplete", "failed", "cancelled", "interrupted", "archived"}
        if status not in allowed:
            raise ValueError(f"Unsupported conversation status: {status}")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET status=?,updated_at=? WHERE id=?",
                (status, time.time(), thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Conversation not found: {thread_id}")

    def queue_worker(self, thread_id: str) -> bool:
        """Atomically claim a worker so multiple WebViews cannot run it twice."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET status='queued',updated_at=? WHERE id=? "
                "AND kind IN ('main','chat','worker','review','research') "
                "AND status IN ('idle','completed','failed','cancelled','interrupted')",
                (time.time(), thread_id),
            )
            return cursor.rowcount == 1

    def claim_foreground(self, thread_id: str) -> bool:
        """Atomically reserve a conversation for one foreground model turn."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE threads SET status='running',updated_at=? WHERE id=? "
                "AND archived=0 AND status IN "
                "('idle','completed','failed','cancelled','interrupted','incomplete')",
                (time.time(), thread_id),
            )
            return cursor.rowcount == 1

    def resumable_workers(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM threads WHERE archived=0 "
                "AND kind IN ('worker','review','research') "
                "AND status IN ('interrupted','failed','cancelled') "
                "ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            runs = self.worker_runs(item["id"])
            state = self.load_state(item["id"])
            item["latest_run"] = runs[0] if runs else None
            item["recovery"] = state.get("recovery") or {}
            result.append(item)
        return result

    def save_images(self, attachments: list, meta: list) -> list:
        """Store validated raster uploads; transcripts contain references, never base64.

        BLOBs participate in the existing SQLite backup. Access is authorized by
        references in a particular conversation, not by a caller-supplied path.
        """
        from tc_agent.attachments import MAX_FILES, VISUAL_MAX_BYTES, MAX_TOTAL_BYTES, _image_media_type
        if len(attachments) != len(meta) or len(meta) > MAX_FILES:
            raise ValueError("Invalid attachment metadata")
        result = [dict(item) for item in meta]
        total = 0
        with self._connect() as db:
            for upload, item in zip(attachments, result):
                if item.get("kind") != "image":
                    continue
                raw = base64.b64decode(upload.get("data_b64", ""), validate=True)
                total += len(raw)
                if not raw or len(raw) > VISUAL_MAX_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError("Invalid image size")
                image_id = uuid.uuid4().hex
                media = _image_media_type(upload.get("name", ""), upload.get("mime", ""))
                digest = hashlib.sha256(raw).hexdigest()
                db.execute("INSERT INTO attachments VALUES(?,?,?,?,?)",
                           (image_id, item["name"], media, digest, raw))
                item.update(attachment_id=image_id, size=len(raw), media_type=media)
        return result

    def read_image(self, thread_id: str, image_id: str) -> dict:
        state = self.load_state(thread_id)
        allowed = any(a.get("attachment_id") == image_id
                      for ev in state["events"] if ev.get("type") == "user"
                      for a in ev.get("attachments", []))
        if not re.fullmatch(r"[0-9a-f]{32}", image_id or "") or not allowed:
            raise ValueError("图片不属于当前对话，或原图未保存")
        with self._connect() as db:
            row = db.execute("SELECT * FROM attachments WHERE id=?", (image_id,)).fetchone()
        if not row or hashlib.sha256(row["data"]).hexdigest() != row["sha256"]:
            raise ValueError("原图不存在或完整性校验失败，请重新上传")
        return {"kind": "image", "name": row["name"], "media_type": row["media_type"],
                "data_b64": base64.b64encode(row["data"]).decode("ascii")}

    def recent_images(self, thread_id: str) -> tuple[list, str]:
        """Reload the latest image-bearing user turn, including after compaction.

        Never silently fall back to an older picture when the newest is missing.
        A batch is bounded by the upload limits and is not added to text history.
        """
        from tc_agent.attachments import MAX_FILES, MAX_TOTAL_BYTES
        state = self.load_state(thread_id)
        for ev in reversed(state["events"]):
            refs = [a for a in ev.get("attachments", []) if a.get("kind") == "image"]
            if ev.get("type") != "user" or not refs:
                continue
            images, missing, total = [], [], 0
            for ref in refs[:MAX_FILES]:
                try:
                    block = self.read_image(thread_id, ref.get("attachment_id", ""))
                    encoded = block["data_b64"]
                    total += len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError("图片批次超限")
                    images.append(block)
                except ValueError:
                    missing.append(ref.get("name", "image"))
            note = "以下图片来自当前对话最近一次图片上传，不是本轮新上传。原始请求：" + str(ev.get("text", ""))[:1000]
            if missing:
                note += "\n原图未保存或不可读取：" + ", ".join(missing) + "。请说明缺失并要求重新上传，不得假装看到了原图。"
            return images, note
        return [], ""

    def load_state(self, thread_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM thread_state WHERE thread_id=?", (thread_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Conversation state not found: {thread_id}")
        return {
            "messages": json.loads(row["messages_json"] or "[]"),
            "events": json.loads(row["events_json"] or "[]"),
            "summary": row["summary"] or "",
            "recovery": json.loads(row["recovery_json"] or "{}"),
        }

    def save_state(self, thread_id: str, state: dict) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "UPDATE thread_state SET messages_json=?,events_json=?,summary=?,recovery_json=? "
                "WHERE thread_id=?",
                (
                    _json(state.get("messages") or []),
                    _json(state.get("events") or []),
                    str(state.get("summary") or ""),
                    _json(state.get("recovery") or {}),
                    thread_id,
                ),
            )
            db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))

    def mutate_state(self, thread_id: str, mutator) -> dict:
        """Atomically mutate one state, merging updates from multiple WebViews."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM thread_state WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Conversation state not found: {thread_id}")
            state = {
                "messages": json.loads(row["messages_json"] or "[]"),
                "events": json.loads(row["events_json"] or "[]"),
                "summary": row["summary"] or "",
                "recovery": json.loads(row["recovery_json"] or "{}"),
            }
            updated = mutator(state) or state
            db.execute(
                "UPDATE thread_state SET messages_json=?,events_json=?,summary=?,recovery_json=? "
                "WHERE thread_id=?",
                (
                    _json(updated.get("messages") or []),
                    _json(updated.get("events") or []),
                    str(updated.get("summary") or ""),
                    _json(updated.get("recovery") or {}),
                    thread_id,
                ),
            )
            db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
        return updated

    def send(
        self, from_thread: str, to_thread: str, payload: dict, *, message_type: str = "task"
    ) -> dict:
        self.get_thread(from_thread)
        self.get_thread(to_thread)
        message_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO mailbox(id,from_thread,to_thread,message_type,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (message_id, from_thread, to_thread, message_type, _json(payload), now),
            )
        return {"id": message_id, "from_thread": from_thread, "to_thread": to_thread,
                "message_type": message_type, "payload": payload, "created_at": now}

    def inbox(self, thread_id: str, *, unread_only: bool = False) -> list[dict]:
        query = "SELECT * FROM mailbox WHERE to_thread=?"
        params: list = [thread_id]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at ASC"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            messages.append(item)
        return messages

    def mark_read(self, thread_id: str, message_ids: list[str]) -> None:
        ids = [str(item) for item in message_ids if item]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            db.execute(
                f"UPDATE mailbox SET read_at=? WHERE to_thread=? AND id IN ({placeholders})",
                [time.time(), thread_id, *ids],
            )

    def create_proposal(
        self, worker_thread: str, parent_thread: str, tool_name: str,
        args: dict, rationale: str = "",
    ) -> dict:
        self.get_thread(worker_thread)
        self.get_thread(parent_thread)
        proposal_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO change_proposals"
                "(id,worker_thread,parent_thread,tool_name,args_json,rationale,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (proposal_id, worker_thread, parent_thread, tool_name,
                 _json(args), str(rationale or "")[:2000], "pending", now),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM change_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Change proposal not found: {proposal_id}")
        item = dict(row)
        item["args"] = json.loads(item.pop("args_json") or "{}")
        item["result"] = json.loads(item.pop("result_json") or "null")
        return item

    def list_proposals(self, parent_thread: str, *, status: str = "pending") -> list[dict]:
        query = "SELECT id FROM change_proposals WHERE parent_thread=?"
        params: list = [parent_thread]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        with self._connect() as db:
            ids = [row["id"] for row in db.execute(query, params).fetchall()]
        return [self.get_proposal(item) for item in ids]

    def resolve_proposal(self, proposal_id: str, status: str, result=None) -> dict:
        if status not in {"approved", "rejected", "failed"}:
            raise ValueError(f"Unsupported proposal resolution: {status}")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE change_proposals SET status=?,result_json=?,resolved_at=? "
                "WHERE id=? AND status='pending'",
                (status, _json(result), time.time(), proposal_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("提案不存在或已处理")
        return self.get_proposal(proposal_id)

    def start_worker_run(self, thread_id: str, provider_model: str = "") -> str:
        self.get_thread(thread_id)
        run_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO worker_runs(id,thread_id,provider_model,started_at) VALUES(?,?,?,?)",
                (run_id, thread_id, str(provider_model or "")[:200], time.time()),
            )
        return run_id

    def finish_worker_run(
        self, run_id: str, status: str, *, model_calls: int = 0,
        tokens_in: int = 0, tokens_out: int = 0, error: str = "",
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE worker_runs SET status=?,model_calls=?,tokens_in=?,tokens_out=?,"
                "finished_at=?,error=? WHERE id=?",
                (status, int(model_calls), int(tokens_in), int(tokens_out),
                 time.time(), str(error or "")[:1000], run_id),
            )

    def worker_runs(self, thread_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM worker_runs WHERE thread_id=? ORDER BY started_at DESC",
                (thread_id,),
            ).fetchall()]

    # ------------------------------------------------------------------
    # Durable foreground execution ledger
    # ------------------------------------------------------------------
    def start_agent_run(
        self, thread_id: str, request_text: str, *, provider_model: str = "",
        target_pid: int = 0, solution: str = "",
    ) -> str:
        """Start one user-request run independently of UI message history."""
        self.get_thread(thread_id)
        run_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO agent_runs"
                "(id,thread_id,request_text,provider_model,target_pid,solution,status,"
                "started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id, thread_id, str(request_text or "")[:20000],
                    str(provider_model or "")[:200], int(target_pid or 0),
                    str(solution or "")[:2000], "running", now, now,
                ),
            )
        return run_id

    def finish_agent_run(
        self, run_id: str, status: str, *, model_calls: int = 0,
        tokens_in: int = 0, tokens_out: int = 0, error: str = "",
    ) -> None:
        terminal = {"completed", "incomplete", "failed", "cancelled", "interrupted"}
        if status not in terminal:
            raise ValueError(f"Unsupported Agent run status: {status}")
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE agent_runs SET status=?,model_calls=?,tokens_in=?,tokens_out=?,"
                "updated_at=?,finished_at=?,error=? WHERE id=? AND status='running'",
                (
                    status, int(model_calls), int(tokens_in), int(tokens_out),
                    now, now, str(error or "")[:4000], run_id,
                ),
            )
            if cursor.rowcount != 1:
                row = db.execute(
                    "SELECT status FROM agent_runs WHERE id=?", (run_id,)
                ).fetchone()
                if not row:
                    raise KeyError(f"Agent run not found: {run_id}")
                if row["status"] != status:
                    raise ValueError(
                        f"Agent run is already terminal: {row['status']} -> {status}"
                    )

    def get_agent_run(self, run_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Agent run not found: {run_id}")
        return dict(row)

    def agent_runs(self, thread_id: str, *, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM agent_runs WHERE thread_id=? "
                "ORDER BY started_at DESC LIMIT ?",
                (thread_id, max(1, min(500, int(limit)))),
            ).fetchall()]

    def start_agent_step(
        self, run_id: str, sequence: int, kind: str, input_payload=None,
    ) -> str:
        step_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO agent_steps"
                "(id,run_id,sequence,kind,status,input_json,started_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    step_id, run_id, int(sequence), str(kind or "model")[:40],
                    "running", _json(input_payload if input_payload is not None else {}), now,
                ),
            )
            db.execute(
                "UPDATE agent_runs SET current_step=?,updated_at=? WHERE id=?",
                (int(sequence), now, run_id),
            )
        return step_id

    def finish_agent_step(
        self, step_id: str, status: str, *, output_payload=None, error: str = "",
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"Unsupported Agent step status: {status}")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE agent_steps SET status=?,output_json=?,finished_at=?,error=? "
                "WHERE id=? AND status='running'",
                (
                    status,
                    _json(output_payload) if output_payload is not None else None,
                    time.time(), str(error or "")[:4000], step_id,
                ),
            )
            if cursor.rowcount != 1:
                row = db.execute(
                    "SELECT status FROM agent_steps WHERE id=?", (step_id,)
                ).fetchone()
                if not row:
                    raise KeyError(f"Agent step not found: {step_id}")
                if row["status"] != status:
                    raise ValueError(
                        f"Agent step is already terminal: {row['status']} -> {status}"
                    )

    @staticmethod
    def _decode_tool_execution(row) -> dict:
        item = dict(row)
        item["args"] = json.loads(item.pop("args_json") or "{}")
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        item["readonly"] = bool(item.get("readonly"))
        return item

    def begin_tool_execution(
        self, *, run_id: str, step_id: str, tool_call_id: str,
        tool_name: str, args: dict, category: str, danger: str,
        readonly: bool, target_pid: int, idempotency_key: str,
    ) -> dict:
        """Atomically reserve an action or return its prior durable outcome.

        ``disposition`` is ``execute`` for a new reservation, ``replay`` for a
        terminal prior result, and ``uncertain`` when execution may already
        have reached XAE. The caller must never auto-run an uncertain action.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
            if existing:
                decoded = self._decode_tool_execution(existing)
                disposition = (
                    "replay" if decoded["status"] in {"completed", "failed", "denied"}
                    else "uncertain"
                )
                return {"disposition": disposition, "execution": decoded}
            if not readonly:
                candidates = db.execute(
                    "SELECT * FROM tool_executions WHERE readonly=0 "
                    "AND target_pid=? AND tool_name=? "
                    "AND status IN ('running','uncertain') ORDER BY started_at",
                    (int(target_pid or 0), str(tool_name)),
                ).fetchall()
                unresolved = next((row for row in candidates
                                   if json.loads(row["args_json"] or "{}") == args), None)
                if unresolved:
                    return {"disposition": "uncertain",
                            "execution": self._decode_tool_execution(unresolved)}
            execution_id = uuid.uuid4().hex
            now = time.time()
            db.execute(
                "INSERT INTO tool_executions"
                "(id,run_id,step_id,tool_call_id,tool_name,args_json,category,danger,"
                "readonly,target_pid,idempotency_key,status,started_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id, run_id, step_id or None, str(tool_call_id),
                    str(tool_name), _json(args), str(category or ""), str(danger or ""),
                    int(bool(readonly)), int(target_pid or 0), str(idempotency_key),
                    "running", now,
                ),
            )
            row = db.execute(
                "SELECT * FROM tool_executions WHERE id=?", (execution_id,)
            ).fetchone()
        return {"disposition": "execute", "execution": self._decode_tool_execution(row)}

    def finish_tool_execution(
        self, execution_id: str, status: str, *, result=None, error: str = "",
    ) -> dict:
        if status not in {"completed", "failed", "denied", "uncertain"}:
            raise ValueError(f"Unsupported tool execution status: {status}")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tool_executions SET status=?,result_json=?,finished_at=?,error=? "
                "WHERE id=? AND status='running'",
                (
                    status, _json(result), time.time(), str(error or "")[:4000],
                    execution_id,
                ),
            )
            if cursor.rowcount != 1:
                existing = db.execute(
                    "SELECT * FROM tool_executions WHERE id=?", (execution_id,)
                ).fetchone()
                if not existing:
                    raise KeyError(f"Tool execution not found: {execution_id}")
                if existing["status"] != status:
                    raise ValueError(
                        "Tool execution is already terminal: "
                        f"{existing['status']} -> {status}"
                    )
            row = db.execute(
                "SELECT * FROM tool_executions WHERE id=?", (execution_id,)
            ).fetchone()
        return self._decode_tool_execution(row)

    def create_approval(
        self, run_id: str, request: dict, *, tool_execution_id: str = "",
        approval_id: str = "",
    ) -> str:
        item_id = approval_id or uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO approvals"
                "(id,run_id,tool_execution_id,status,request_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    item_id, run_id, tool_execution_id or None, "pending",
                    _json(request), time.time(),
                ),
            )
        return item_id

    def resolve_approval(self, approval_id: str, allow: bool, reason: str = "") -> dict:
        decision = {"allow": bool(allow), "reason": str(reason or "")}
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE approvals SET status=?,decision_json=?,resolved_at=? "
                "WHERE id=? AND status='pending'",
                (
                    "approved" if allow else "rejected", _json(decision),
                    time.time(), approval_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Approval does not exist or is already resolved")
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json") or "{}")
        item["decision"] = json.loads(item.pop("decision_json") or "null")
        return item

    # ------------------------------------------------------------------
    # Structured authorization plans
    # ------------------------------------------------------------------
    def create_authorization_plan(self, plan: dict) -> dict:
        """Persist one short-lived plan per thread; never overwrite a plan."""
        from tc_agent.authorization import AuthorizationError

        if not isinstance(plan, dict):
            raise AuthorizationError("授权计划必须是对象")
        plan_id = str(plan.get("id") or "").strip()
        thread_id = str((plan.get("context") or {}).get("thread_id") or "").strip()
        if not plan_id or not thread_id:
            raise AuthorizationError("授权计划缺少 id 或 thread_id")
        self.get_thread(thread_id)
        now = time.time()
        expires_at = float(plan.get("expires_at") or 0)
        if expires_at <= now:
            raise AuthorizationError("授权计划已过期")
        with self._connect() as db:
            db.execute(
                "UPDATE authorization_plans SET status='expired',invalidated_at=? "
                "WHERE thread_id=? AND status='pending' AND expires_at<=?",
                (now, thread_id, now),
            )
            try:
                db.execute(
                    "INSERT INTO authorization_plans"
                    "(id,thread_id,status,plan_json,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, thread_id, "pending", _json(plan),
                     float(plan.get("created_at") or now), expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthorizationError(
                    "该对话已有未消费的授权计划，必须先确认、拒绝或等待其过期"
                ) from exc
        return dict(plan)

    @staticmethod
    def _decode_authorization_row(row) -> dict | None:
        if row is None:
            return None
        try:
            plan = json.loads(row["plan_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        plan["id"] = str(row["id"])
        plan["thread_id"] = str(row["thread_id"])
        plan["status"] = str(row["status"])
        plan["created_at"] = float(row["created_at"] or 0)
        plan["expires_at"] = float(row["expires_at"] or 0)
        if row["confirmed_at"] is not None:
            plan["confirmed_at"] = float(row["confirmed_at"])
        if row["invalidated_at"] is not None:
            plan["invalidated_at"] = float(row["invalidated_at"])
        return plan

    def get_authorization_plan(self, plan_id: str) -> dict | None:
        current = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM authorization_plans WHERE id=?", (str(plan_id or ""),)
            ).fetchone()
            if (row is not None and row["status"] == "pending"
                    and float(row["expires_at"] or 0) <= current):
                db.execute(
                    "UPDATE authorization_plans SET status='expired',invalidated_at=? "
                    "WHERE id=? AND status='pending'",
                    (current, row["id"]),
                )
                row = db.execute(
                    "SELECT * FROM authorization_plans WHERE id=?", (str(plan_id or ""),)
                ).fetchone()
        return self._decode_authorization_row(row)

    def pending_authorization_plan(self, thread_id: str, *, now: float | None = None) -> dict | None:
        current = float(time.time() if now is None else now)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM authorization_plans WHERE thread_id=? AND status='pending' "
                "ORDER BY created_at DESC",
                (str(thread_id or ""),),
            ).fetchall()
            for row in rows:
                if float(row["expires_at"] or 0) <= current:
                    db.execute(
                        "UPDATE authorization_plans SET status='expired',invalidated_at=? "
                        "WHERE id=? AND status='pending'",
                        (current, row["id"]),
                    )
                    continue
                return self._decode_authorization_row(row)
        return None

    def confirm_authorization_plan(
        self, plan_id: str, *, selection=None, now: float | None = None,
    ) -> dict:
        """Atomically consume confirmation and optionally approve an exact subset."""
        from tc_agent.authorization import AuthorizationError, select_actions

        current = float(time.time() if now is None else now)
        expired = False
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM authorization_plans WHERE id=?", (str(plan_id or ""),)
            ).fetchone()
            if row is None:
                raise KeyError(f"授权计划不存在: {plan_id}")
            if row["status"] != "pending":
                raise AuthorizationError("授权计划已消费、失效或拒绝，不能重复确认")
            if float(row["expires_at"] or 0) <= current:
                db.execute(
                    "UPDATE authorization_plans SET status='expired',invalidated_at=? "
                    "WHERE id=? AND status='pending'", (current, row["id"]),
                )
                expired = True
            if expired:
                # Leave the transaction cleanly so the terminal expiry state
                # is durable before reporting the rejection.
                pass
            else:
                plan = self._decode_authorization_row(row)
                try:
                    approved = select_actions(plan, selection)
                except (AuthorizationError, TypeError) as exc:
                    raise AuthorizationError(str(exc)) from exc
                plan["status"] = "authorized"
                plan["approved_actions"] = approved
                plan["confirmation_consumed_at"] = current
                updated = db.execute(
                    "UPDATE authorization_plans SET status='authorized',plan_json=?,confirmed_at=? "
                    "WHERE id=? AND status='pending'",
                    (_json(plan), current, row["id"]),
                )
                if updated.rowcount != 1:
                    raise AuthorizationError("授权计划已被另一确认请求消费")
        if expired:
            raise AuthorizationError("授权计划已过期，请重新提出计划")
        return plan

    def record_authorization_action(
        self, plan_id: str, name: str, args: dict | None = None, *,
        outcome: str = "completed",
    ) -> dict:
        """Record one concrete action; failures and uncertain results are terminal."""
        from tc_agent.authorization import (
            AuthorizationError, consume_action, expand_actions, plan_allows_action,
        )

        outcome = str(outcome or "").lower()
        if outcome not in {"completed", "failed", "uncertain"}:
            raise ValueError(f"unsupported authorization action outcome: {outcome}")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM authorization_plans WHERE id=?", (str(plan_id or ""),)
            ).fetchone()
            if row is None:
                raise KeyError(f"授权计划不存在: {plan_id}")
            plan = self._decode_authorization_row(row)
            if plan.get("status") != "authorized":
                raise AuthorizationError("授权计划未处于可执行状态")
            if not plan_allows_action(plan, name, args):
                raise AuthorizationError("该工具动作不在剩余的已确认清单中，禁止重放或扩权")
            updated_plan = consume_action(plan, name, args, success=outcome == "completed")
            if outcome == "uncertain":
                keys = [item["key"] for item in expand_actions(name, args)]
                updated_plan["uncertain_action_keys"] = list(
                    dict.fromkeys(list(updated_plan.get("uncertain_action_keys") or []) + keys)
                )
                updated_plan["status"] = "uncertain"
            new_status = str(updated_plan.get("status") or "authorized")
            db.execute(
                "UPDATE authorization_plans SET status=?,plan_json=? WHERE id=? AND status='authorized'",
                (new_status, _json(updated_plan), row["id"]),
            )
        return updated_plan

    def invalidate_authorization_plans(
        self, *, thread_id: str | None = None, reason: str = "上下文已变化",
    ) -> int:
        """Invalidate pending/authorized plans without touching conversation history."""
        where = "status IN ('pending','authorized')"
        values: list[object] = []
        if thread_id:
            where += " AND thread_id=?"
            values.append(str(thread_id))
        now = time.time()
        with self._connect() as db:
            rows = db.execute(f"SELECT id,plan_json FROM authorization_plans WHERE {where}", values).fetchall()
            for row in rows:
                try:
                    plan = json.loads(row["plan_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    plan = {}
                plan["status"] = "invalidated"
                plan["invalidation_reason"] = str(reason or "上下文已变化")[:1000]
                db.execute(
                    "UPDATE authorization_plans SET status='invalidated',plan_json=?,invalidated_at=? WHERE id=?",
                    (_json(plan), now, row["id"]),
                )
        return len(rows)

    def authorization_plans(self, thread_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM authorization_plans"
        values: tuple[object, ...] = ()
        if thread_id:
            query += " WHERE thread_id=?"
            values = (str(thread_id),)
        query += " ORDER BY created_at"
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [self._decode_authorization_row(row) for row in rows]

    def agent_run_snapshot(self, run_id: str) -> dict:
        run = self.get_agent_run(run_id)
        with self._connect() as db:
            step_rows = db.execute(
                "SELECT * FROM agent_steps WHERE run_id=? ORDER BY sequence,started_at",
                (run_id,),
            ).fetchall()
            tool_rows = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=? ORDER BY started_at",
                (run_id,),
            ).fetchall()
            approval_rows = db.execute(
                "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        steps = []
        for row in step_rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json") or "{}")
            raw = item.pop("output_json")
            item["output"] = json.loads(raw) if raw else None
            steps.append(item)
        approvals = []
        for row in approval_rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json") or "{}")
            raw = item.pop("decision_json")
            item["decision"] = json.loads(raw) if raw else None
            approvals.append(item)
        return {
            "run": run,
            "steps": steps,
            "tool_executions": [self._decode_tool_execution(row) for row in tool_rows],
            "approvals": approvals,
        }

    def collect_children(self, parent_thread: str) -> dict:
        self.get_thread(parent_thread)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM threads WHERE parent_id=? AND archived=0 ORDER BY created_at ASC",
                (parent_thread,),
            ).fetchall()
        children = []
        for row in rows:
            child = dict(row)
            runs = self.worker_runs(child["id"])
            results = [item for item in self.inbox(parent_thread)
                       if item["from_thread"] == child["id"]
                       and item["message_type"] in {"task_result", "notice"}]
            child["latest_run"] = runs[0] if runs else None
            child["results"] = results[-3:]
            children.append(child)
        done = all(item["status"] not in {"queued", "running"} for item in children)
        return {"parent_thread": parent_thread, "all_done": done, "children": children}

    def remember(
        self, source_thread: str, memory_key: str, category: str,
        content: str, confidence: float = 1.0,
    ) -> dict:
        self.get_thread(source_thread)
        key = str(memory_key or "").strip().lower()
        text = str(content or "").strip()
        allowed = {"fact", "decision", "preference", "constraint"}
        if not key or len(key) > 120:
            raise ValueError("记忆键不能为空且不能超过 120 字符")
        if category not in allowed:
            raise ValueError(f"不支持的记忆类别: {category}")
        if not text or len(text) > 2000:
            raise ValueError("记忆内容不能为空且不能超过 2000 字符")
        if _SECRET_PATTERN.search(text):
            raise ValueError("记忆内容疑似包含 API Key、授权码、密码或令牌，已拒绝保存")
        score = max(0.0, min(1.0, float(confidence)))
        now = time.time()
        with self._connect() as db:
            existing = db.execute(
                "SELECT id,content FROM project_memories WHERE memory_key=? AND active=1",
                (key,),
            ).fetchone()
            if existing and existing["content"] != text:
                return {
                    "conflict": True, "memory_key": key,
                    "existing_id": existing["id"], "existing_content": existing["content"],
                    "proposed_content": text,
                }
            if existing:
                memory_id = existing["id"]
                db.execute(
                    "UPDATE project_memories SET category=?,source_thread=?,confidence=?,updated_at=? WHERE id=?",
                    (category, source_thread, score, now, memory_id),
                )
            else:
                memory_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO project_memories"
                    "(id,memory_key,category,content,source_thread,confidence,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (memory_id, key, category, text, source_thread, score, now, now),
                )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM project_memories WHERE id=?", (memory_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Memory not found: {memory_id}")
        return dict(row)

    def memories(self, *, active_only: bool = True, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM project_memories"
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY category,memory_key LIMIT ?"
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, (max(1, min(500, limit)),)).fetchall()]

    def forget(self, memory_id: str) -> dict:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE project_memories SET active=0,updated_at=? WHERE id=? AND active=1",
                (time.time(), memory_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Active memory not found: {memory_id}")
        return self.get_memory(memory_id)

    def memory_prompt(self, max_chars: int = 8000) -> str:
        lines = []
        used = 0
        for item in self.memories(limit=200):
            line = f"- [{item['category']}] {item['memory_key']}: {item['content']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def health(self) -> dict:
        """Return non-mutating database integrity and usage statistics."""
        with self._connect() as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            counts = {
                table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "threads", "mailbox", "change_proposals",
                    "worker_runs", "project_memories", "agent_runs",
                    "agent_steps", "tool_executions", "approvals", "authorization_plans",
                )
            }
            schema = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        return {
            "ok": integrity.lower() == "ok",
            "integrity": integrity,
            "schema_version": int(schema[0]) if schema else 0,
            "database": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "counts": counts,
        }

    def backup(self) -> dict:
        """Create a consistent SQLite snapshot inside the project data folder."""
        backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = backup_dir / f"agent-{stamp}-{uuid.uuid4().hex[:6]}.db"
        source = sqlite3.connect(self.db_path, timeout=10.0)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return {"backup": str(destination), "size_bytes": destination.stat().st_size}

    def export_json(self) -> dict:
        """Export project conversations and audit records as readable JSON."""
        export_dir = self.db_path.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = export_dir / f"agent-export-{stamp}-{uuid.uuid4().hex[:6]}.json"
        with self._connect() as db:
            payload = {
                "format": "twincat-agent-project-export-v1",
                "exported_at": time.time(),
                "schema_version": SCHEMA_VERSION,
                "threads": [dict(row) for row in db.execute(
                    "SELECT * FROM threads ORDER BY created_at"
                )],
                "thread_state": [dict(row) for row in db.execute(
                    "SELECT * FROM thread_state ORDER BY thread_id"
                )],
                "mailbox": [dict(row) for row in db.execute(
                    "SELECT * FROM mailbox ORDER BY created_at"
                )],
                "change_proposals": [dict(row) for row in db.execute(
                    "SELECT * FROM change_proposals ORDER BY created_at"
                )],
                "worker_runs": [dict(row) for row in db.execute(
                    "SELECT * FROM worker_runs ORDER BY started_at"
                )],
                "agent_runs": [dict(row) for row in db.execute(
                    "SELECT * FROM agent_runs ORDER BY started_at"
                )],
                "agent_steps": [dict(row) for row in db.execute(
                    "SELECT * FROM agent_steps ORDER BY started_at"
                )],
                "tool_executions": [dict(row) for row in db.execute(
                    "SELECT * FROM tool_executions ORDER BY started_at"
                )],
                "approvals": [dict(row) for row in db.execute(
                    "SELECT * FROM approvals ORDER BY created_at"
                )],
                "authorization_plans": [dict(row) for row in db.execute(
                    "SELECT * FROM authorization_plans ORDER BY created_at"
                )],
                "project_memories": [dict(row) for row in db.execute(
                    "SELECT * FROM project_memories ORDER BY created_at"
                )],
            }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "export": str(destination), "size_bytes": destination.stat().st_size,
            "warning": "导出包含对话正文，请按项目资料妥善保管。",
        }


class ConversationHistory:
    """History-compatible adapter backed by one SQLite conversation."""

    def __init__(self, store: ConversationStore, thread_id: str, max_events: int = 800) -> None:
        self.store = store
        self.thread_id = thread_id
        self.max_events = max_events
        state = store.load_state(thread_id)
        self.messages = state["messages"]
        self.events = state["events"]
        self.summary = state["summary"]
        self.recovery = state["recovery"]
        self.path = store.db_path

    def _save(self) -> None:
        self.store.save_state(self.thread_id, {
            "messages": self.messages, "events": self.events,
            "summary": self.summary, "recovery": self.recovery,
        })

    def _apply(self, mutator) -> dict:
        state = self.store.mutate_state(self.thread_id, mutator)
        self.messages = state["messages"]
        self.events = state["events"]
        self.summary = state["summary"]
        self.recovery = state["recovery"]
        return state

    def refresh(self) -> None:
        state = self.store.load_state(self.thread_id)
        self.messages = state["messages"]
        self.events = state["events"]
        self.summary = state["summary"]
        self.recovery = state["recovery"]

    def add_event(self, event: dict) -> dict:
        event = dict(event or {})
        event.setdefault("event_id", uuid.uuid4().hex)

        def append(state):
            if any(str(item.get("event_id") or "") == event["event_id"]
                   for item in state["events"] if isinstance(item, dict)):
                return state
            state["events"].append(event)
            if len(state["events"]) > self.max_events:
                state["events"] = state["events"][-self.max_events:]
            return state
        self._apply(append)
        return event

    def add_message(self, message: dict) -> None:
        self._apply(lambda state: state["messages"].append(message) or state)

    def replace_last_assistant(self, text: str) -> None:
        def replace(state):
            for index in range(len(state["messages"]) - 1, -1, -1):
                message = state["messages"][index]
                if message.get("role") == "assistant" and not message.get("tool_calls"):
                    state["messages"][index] = {**message, "text": text}
                    break
            return state
        self._apply(replace)

    def clear(self) -> None:
        self._apply(lambda state: {
            "messages": [], "events": [], "recovery": {}, "summary": "",
        })

    def clear_recovery(self) -> None:
        self._apply(lambda state: {**state, "recovery": {}})

    def set_recovery(self, request: str, error: str, partial: str = "") -> None:
        from tc_agent.recovery import make_recovery
        def update(state):
            state["recovery"] = make_recovery(state["messages"], request, error, partial, state.get("recovery"))
            return state
        self._apply(update)

    def repair_tool_results(self, repair) -> None:
        self._apply(lambda state: {**state, "messages": repair(state["messages"])})

    def commit_compaction(self, source_messages: list, messages: list, summary: str) -> bool:
        """Replace a known history prefix without losing concurrent appends."""
        committed = [False]

        def compact(state):
            current = state["messages"]
            if current[:len(source_messages)] != source_messages:
                return state
            state["messages"] = list(messages) + current[len(source_messages):]
            state["summary"] = summary
            committed[0] = True
            return state

        self._apply(compact)
        return committed[0]

    def resume_prompt(self, user_text: str) -> str:
        normalized = "".join(str(user_text or "").strip().lower().split())
        if not self.recovery or normalized not in {
            "继续", "继续做", "继续执行", "接着做", "接着执行", "恢复", "重试",
            "continue", "resume", "retry",
        }:
            return user_text
        from tc_agent.recovery import resume_text
        return resume_text(self.recovery, user_text)


@contextmanager
def conversation_context(store: ConversationStore, thread_id: str):
    store_token = _active_store.set(store)
    thread_token = _active_thread.set(thread_id)
    try:
        yield
    finally:
        _active_thread.reset(thread_token)
        _active_store.reset(store_token)


def _current() -> tuple[ConversationStore, str]:
    store, thread_id = _active_store.get(), _active_thread.get()
    if store is None or not thread_id:
        raise RuntimeError("Conversation tools require an active project conversation")
    return store, thread_id


def active_conversation_snapshot() -> dict:
    """Return bounded current-thread context for local diagnostic reports."""
    store, thread_id = _current()
    state = store.load_state(thread_id)
    recent_runs = store.agent_runs(thread_id, limit=5)
    return {
        "database": str(store.db_path),
        "thread": store.get_thread(thread_id),
        "events": list(state.get("events") or [])[-60:],
        "execution_trace": [store.agent_run_snapshot(item["id"])
                            for item in reversed(recent_runs)],
    }


def tool_thread_list() -> dict:
    store, current = _current()
    return {"current_thread": current, "threads": store.list_threads()}


def tool_thread_create(args: dict) -> dict:
    store, current = _current()
    thread = store.create_thread(
        str(args.get("title") or "子任务"),
        kind=str(args.get("kind") or "worker"),
        parent_id=current,
    )
    objective = str(args.get("objective") or "").strip()
    if objective:
        store.send(current, thread["id"], {
            "objective": objective,
            "constraints": list(args.get("constraints") or []),
            "expected_output": str(args.get("expected_output") or ""),
        })
    return {"created": thread, "objective_sent": bool(objective)}


def tool_thread_send(args: dict) -> dict:
    store, current = _current()
    payload = {
        "objective": str(args.get("objective") or ""),
        "context": dict(args.get("context") or {}),
        "constraints": list(args.get("constraints") or []),
        "expected_output": str(args.get("expected_output") or ""),
    }
    return store.send(
        current, str(args.get("to_thread") or ""), payload,
        message_type=str(args.get("message_type") or "task"),
    )


def tool_thread_inbox(args: dict) -> dict:
    store, current = _current()
    thread_id = str(args.get("thread_id") or current)
    return {"thread_id": thread_id,
            "messages": store.inbox(thread_id, unread_only=bool(args.get("unread_only", False)))}


def tool_thread_collect(args: dict) -> dict:
    store, current = _current()
    parent_id = str(args.get("parent_thread") or current)
    return store.collect_children(parent_id)


def tool_thread_rename(args: dict) -> dict:
    store, current = _current()
    thread_id = str(args.get("thread_id") or current)
    if thread_id != current:
        target = store.get_thread(thread_id)
        if target.get("parent_id") != current:
            raise ValueError("只能重命名当前对话或其直属子对话")
    return store.rename_thread(thread_id, str(args.get("title") or ""))


def tool_thread_fork(args: dict) -> dict:
    store, current = _current()
    return store.fork_thread(current, str(args.get("title") or ""))


def tool_thread_handoff(args: dict) -> dict:
    store, current = _current()
    thread_id = str(args.get("thread_id") or "")
    target = store.get_thread(thread_id)
    if target.get("parent_id") != current:
        raise ValueError("只能转交当前对话的直属子任务")
    return store.reparent_thread(thread_id, str(args.get("new_parent_thread") or ""))


def tool_memory_list(args: dict) -> dict:
    store, _ = _current()
    return {"memories": store.memories(limit=int(args.get("limit") or 100))}


def tool_memory_remember(args: dict) -> dict:
    store, current = _current()
    return store.remember(
        current, str(args.get("memory_key") or ""),
        str(args.get("category") or "fact"), str(args.get("content") or ""),
        float(args.get("confidence", 1.0)),
    )


def tool_memory_forget(args: dict) -> dict:
    store, _ = _current()
    return store.forget(str(args.get("memory_id") or ""))


def tool_project_data_health() -> dict:
    store, _ = _current()
    return store.health()


def tool_project_data_backup() -> dict:
    store, _ = _current()
    return store.backup()


def tool_project_data_export() -> dict:
    store, _ = _current()
    return store.export_json()
