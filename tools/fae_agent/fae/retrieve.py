from __future__ import annotations

import html
import re
import sqlite3
import zipfile
from pathlib import Path


ROOT = Path(r"G:\Codex\Beckhoff-Virtual-Academy")
DB = Path(__file__).resolve().parents[1] / "data" / "fae.sqlite3"
TEXT_EXTENSIONS = {".html", ".htm", ".txt", ".md", ".csv", ".json", ".xml"}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*|[\u4e00-\u9fff]{1,8}|\d+")


def _clean(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _read(path: Path) -> str:
    try:
        if path.suffix.lower() in TEXT_EXTENSIONS:
            return _clean(path.read_text(encoding="utf-8", errors="ignore"))
        if path.suffix.lower() in OFFICE_EXTENSIONS:
            with zipfile.ZipFile(path) as archive:
                parts = []
                for name in archive.namelist():
                    if name.endswith(".xml") and any(
                        key in name for key in ("document.xml", "slides/slide", "sharedStrings.xml")
                    ):
                        parts.append(_clean(archive.read(name).decode("utf-8", "ignore")))
                return " ".join(parts)
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


def _chunks(text: str, size: int = 1800, overlap: int = 250):
    start = 0
    while start < len(text):
        yield text[start : start + size]
        if start + size >= len(text):
            break
        start += size - overlap


def rebuild(prune: bool = False) -> dict:
    if not ROOT.exists():
        raise FileNotFoundError(f"中文语料目录不存在: {ROOT}")
    DB.parent.mkdir(parents=True, exist_ok=True)
    if prune and DB.exists():
        DB.unlink()
    connection = sqlite3.connect(DB)
    connection.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
          locator UNINDEXED, title, content, tokenize='unicode61'
        );
        DELETE FROM chunks;
        """
    )
    files = chunk_count = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS | OFFICE_EXTENSIONS:
            continue
        content = _read(path)
        if not content:
            continue
        relative = str(path.relative_to(ROOT))
        title = " ".join(path.parts[-4:])
        for part in _chunks(content):
            connection.execute(
                "INSERT INTO chunks(locator,title,content) VALUES(?,?,?)",
                (relative, title, part),
            )
            chunk_count += 1
        files += 1
    connection.commit()
    connection.close()
    return {"files": files, "chunks": chunk_count, "database": str(DB)}


def _query(value: str) -> str:
    terms = WORD.findall(value)
    return " OR ".join(f'"{term}"' for term in terms[:20])


def search(query: str, limit: int = 6) -> list[dict]:
    if not DB.exists():
        rebuild()
    expression = _query(query)
    if not expression:
        return []
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT locator, title,
               snippet(chunks, 2, '【', '】', ' … ', 36) AS excerpt,
               bm25(chunks, 0.0, 2.0, 1.0) AS score
        FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?
        """,
        (expression, limit),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def read_source(locator: str) -> str:
    path = (ROOT / locator).resolve()
    if ROOT.resolve() not in path.parents:
        raise ValueError("locator 超出中文语料目录")
    if not path.is_file():
        raise FileNotFoundError(path)
    return _read(path)
