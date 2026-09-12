"""TwinCAT Agent 供应商发码记录的本地 SQLite 存储。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def default_records_path() -> Path:
    override = os.environ.get("TC_AGENT_LICENSE_RECORDS_DB", "").strip()
    if override:
        return Path(override)
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "TwinCAT Agent License Tool" / "license_records.db"


class LicenseRecordStore:
    """按授权编号保存和查询已签发授权，不保存 RSA 私钥。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_records_path()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS license_records (
                    license_id TEXT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_license_customer "
                "ON license_records(customer)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_license_device "
                "ON license_records(device_id)"
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(license_records)"
                ).fetchall()
            }
            if "application_json" not in columns:
                connection.execute(
                    "ALTER TABLE license_records ADD COLUMN application_json TEXT NOT NULL DEFAULT ''"
                )

    def save(self, code: str, payload: dict, application: dict | None = None) -> None:
        required = ("license_id", "customer", "device_id", "issued_at", "expires_at")
        missing = [name for name in required if name not in payload]
        if missing:
            raise ValueError(f"授权记录缺少字段：{', '.join(missing)}")
        if not code.startswith("TCAG1."):
            raise ValueError("授权码格式不正确，无法保存记录")
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO license_records (
                    license_id, customer, device_id, issued_at,
                    expires_at, code, payload_json, application_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(license_id) DO UPDATE SET
                    customer = excluded.customer,
                    device_id = excluded.device_id,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at,
                    code = excluded.code,
                    payload_json = excluded.payload_json,
                    application_json = excluded.application_json
                """,
                (
                    payload["license_id"],
                    payload["customer"],
                    payload["device_id"],
                    payload["issued_at"],
                    payload["expires_at"],
                    code,
                    payload_json,
                    json.dumps(application or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def search(self, query: str = "", limit: int = 500) -> list[dict]:
        limit = max(1, min(int(limit), 2000))
        query = (query or "").strip()
        sql = (
            "SELECT license_id, customer, device_id, issued_at, expires_at, "
            "code, payload_json, application_json FROM license_records"
        )
        parameters: list[object] = []
        if query:
            pattern = f"%{query}%"
            sql += (
                " WHERE customer LIKE ? COLLATE NOCASE"
                " OR device_id LIKE ? COLLATE NOCASE"
                " OR license_id LIKE ? COLLATE NOCASE"
            )
            parameters.extend((pattern, pattern, pattern))
        sql += " ORDER BY issued_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def latest_for_device(self, device_id: str) -> dict | None:
        """返回某个 TwinCAT System ID 最近一次签发的授权记录。"""
        device_id = (device_id or "").strip()
        if not device_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT license_id, customer, device_id, issued_at, expires_at,
                       code, payload_json, application_json
                FROM license_records
                WHERE device_id = ? COLLATE NOCASE
                ORDER BY issued_at DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM license_records"
            ).fetchone()
        return int(row["total"])
