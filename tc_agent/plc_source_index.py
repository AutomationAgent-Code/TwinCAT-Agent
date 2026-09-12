"""Persistent, incremental index for saved TwinCAT PLC source.

The index is deliberately a read acceleration layer only.  XAE COM remains the
authority for dirty editor buffers and post-write verification.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from tc_agent.plc_source import read_source_file, source_index


_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS plc_source (
    file TEXT PRIMARY KEY, project TEXT NOT NULL, project_file TEXT NOT NULL,
    logical_path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
    declaration TEXT NOT NULL, implementation TEXT NOT NULL,
    declaration_hash TEXT NOT NULL, implementation_hash TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plc_source_name ON plc_source(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS plc_source_member (
    file TEXT NOT NULL, member TEXT NOT NULL,
    declaration TEXT NOT NULL, implementation TEXT NOT NULL,
    declaration_hash TEXT NOT NULL, implementation_hash TEXT NOT NULL,
    PRIMARY KEY(file, member)
);
CREATE INDEX IF NOT EXISTS ix_plc_source_member_file ON plc_source_member(file);
"""


def _db_path(solution: str | Path) -> Path:
    source = Path(solution).resolve()
    root = source.parent if source.suffix else source
    return root / ".TwinCATAgent" / "plc_source_index.sqlite"


def _connection(solution: str | Path) -> sqlite3.Connection:
    path = _db_path(solution)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(plc_source)")}
    if "member_index_version" not in columns:
        connection.execute(
            "ALTER TABLE plc_source ADD COLUMN member_index_version INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_path_matches(requested: str, logical_path: str) -> bool:
    """Match only an exact saved-source identity, never a fuzzy tree path.

    ``logical_path`` is produced from the PLC project file's Compile Include
    (for example ``PLC1^POUs^MAIN.TcPOU``).  A TIPC path has different
    semantics and must force the caller to use the live COM path instead of
    silently returning a similarly named disk object.
    """
    wanted = str(logical_path or "").replace("/", "^").replace("\\", "^").casefold()
    value = str(requested or "").replace("/", "^").replace("\\", "^").casefold()
    return value == wanted or value.endswith("^" + wanted)


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if _tag(item) == name.casefold()), None)


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext())


def _code(element: ET.Element) -> tuple[str, str]:
    declaration = "\n".join(_text(_child(element, "declaration")).splitlines())
    implementation = ""
    impl = _child(element, "implementation")
    if impl is not None:
        st = next((item for item in impl.iter() if _tag(item) == "st"), None)
        implementation = "\n".join(_text(st if st is not None else impl).splitlines())
    return declaration, implementation


def _members(file: Path, root_name: str) -> list[tuple[str, str, str]]:
    """Extract named PLC members and property accessors from one XML file."""
    document = ET.parse(file).getroot()
    root = next((item for item in document.iter()
                 if str(item.attrib.get("Name") or "").casefold() == root_name.casefold()), document)
    records: list[tuple[str, str, str]] = []

    def visit(element: ET.Element, prefix: str) -> None:
        for child in element:
            tag = _tag(child)
            child_name = str(child.attrib.get("Name") or "").strip()
            member = prefix
            is_member = tag in {"method", "action", "property", "transition"} and bool(child_name)
            if is_member:
                member = f"{prefix}.{child_name}" if prefix else child_name
                declaration, implementation = _code(child)
                records.append((member, declaration, implementation))
            elif tag in {"get", "set"} and prefix:
                member = f"{prefix}.{tag.title()}"
                declaration, implementation = _code(child)
                records.append((member, declaration, implementation))
            visit(child, member if (is_member or tag in {"get", "set"}) else prefix)

    visit(root, "")
    return records


def sync(solution: str | Path) -> dict:
    """Incrementally synchronize saved PLC root objects into project SQLite."""
    entries = source_index(solution)
    known_files = {str(entry.file): entry for entry in entries}
    added = updated = removed = unchanged = 0
    with _LOCK, _connection(solution) as db:
        existing = {
            row["file"]: row for row in db.execute(
                "SELECT file, mtime_ns, size, member_index_version FROM plc_source"
            )
        }
        for file, entry in known_files.items():
            try:
                stat = entry.file.stat()
            except OSError:
                continue
            previous = existing.get(file)
            if previous and int(previous["mtime_ns"]) == stat.st_mtime_ns \
                    and int(previous["size"]) == stat.st_size \
                    and int(previous["member_index_version"] or 0) >= 1:
                unchanged += 1
                continue
            parsed = read_source_file(entry.file, entry.name)
            declaration = str(parsed.get("declaration") or "")
            implementation = str(parsed.get("implementation") or "")
            db.execute("""
                INSERT INTO plc_source(
                    file, project, project_file, logical_path, name, kind,
                    mtime_ns, size, declaration, implementation,
                    declaration_hash, implementation_hash, indexed_at, member_index_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(file) DO UPDATE SET
                    project=excluded.project, project_file=excluded.project_file,
                    logical_path=excluded.logical_path, name=excluded.name,
                    kind=excluded.kind, mtime_ns=excluded.mtime_ns, size=excluded.size,
                    declaration=excluded.declaration, implementation=excluded.implementation,
                    declaration_hash=excluded.declaration_hash,
                    implementation_hash=excluded.implementation_hash,
                    indexed_at=excluded.indexed_at,
                    member_index_version=excluded.member_index_version
            """, (
                file, entry.project, str(entry.project_file), entry.logical_path,
                entry.name, entry.kind, stat.st_mtime_ns, stat.st_size,
                declaration, implementation, _digest(declaration), _digest(implementation),
                time.time(), 1,
            ))
            db.execute("DELETE FROM plc_source_member WHERE file=?", (file,))
            db.executemany("""
                INSERT INTO plc_source_member(
                    file, member, declaration, implementation, declaration_hash, implementation_hash
                ) VALUES(?,?,?,?,?,?)
            """, ((file, member, declaration, implementation,
                    _digest(declaration), _digest(implementation))
                   for member, declaration, implementation in _members(entry.file, entry.name)))
            if previous is None:
                added += 1
            else:
                updated += 1
        stale = set(existing) - set(known_files)
        if stale:
            db.executemany("DELETE FROM plc_source WHERE file=?", ((file,) for file in stale))
            db.executemany("DELETE FROM plc_source_member WHERE file=?", ((file,) for file in stale))
            removed = len(stale)
    return {"added": added, "updated": updated, "removed": removed,
            "unchanged": unchanged, "total": len(known_files), "db": str(_db_path(solution))}


def read(solution: str | Path, name: str, *, path: str = "", member: str = "",
         area: str = "all", _sync_info: dict | None = None) -> dict:
    """Read a saved root PLC object from SQLite, refreshing changed files first."""
    started = time.perf_counter()
    sync_info = _sync_info if _sync_info is not None else sync(solution)
    with _LOCK, _connection(solution) as db:
        rows = list(db.execute(
            "SELECT * FROM plc_source WHERE name = ? COLLATE NOCASE", (name,)
        ))
    if path:
        normalized = path.replace("/", "^").replace("\\", "^").casefold()
        narrowed = [row for row in rows if _source_path_matches(
            normalized, str(row["logical_path"])
        )]
        if not narrowed:
            choices = ", ".join(str(row["logical_path"]) for row in rows[:8])
            raise ValueError(
                "requested path is not a saved-source index path for this PLC object; "
                "use plc_find/live=true for a COM tree path"
                + (f" (saved paths: {choices})" if choices else "")
            )
        rows = narrowed
    if not rows:
        raise FileNotFoundError(f"PLC source object not found in index: {name}")
    if len(rows) > 1:
        choices = ", ".join(str(row["logical_path"]) for row in rows[:8])
        raise ValueError(f"PLC object name is ambiguous: {name}; use path ({choices})")
    row = rows[0]
    member_row = None
    if member:
        with _LOCK, _connection(solution) as db:
            member_row = db.execute("""
                SELECT * FROM plc_source_member WHERE file=? AND member = ? COLLATE NOCASE
            """, (row["file"], member)).fetchone()
        if member_row is None:
            raise FileNotFoundError(f"PLC member not found in index: {member}")
    declaration = member_row["declaration"] if member_row else row["declaration"]
    implementation = member_row["implementation"] if member_row else row["implementation"]
    declaration_hash = member_row["declaration_hash"] if member_row else row["declaration_hash"]
    implementation_hash = member_row["implementation_hash"] if member_row else row["implementation_hash"]
    result = {
        "status": "read", "name": name, "method": member, "project": row["project"],
        "path": row["logical_path"], "file": row["file"],
        "source_path": row["logical_path"], "path_kind": "saved_source",
        "tree_path": "", "tree_path_available": False,
        "project_file": row["project_file"], "kind": row["kind"],
        "source": "disk_index", "live_xae": False, "authoritative": False,
        "dirty_unknown": True, "mtime_ns": row["mtime_ns"], "size": row["size"],
        "hashes": {"declaration": declaration_hash, "implementation": implementation_hash},
        "index_sync": sync_info,
    }
    if area in {"all", "declaration"}:
        result["declaration"] = declaration
    if area in {"all", "implementation"}:
        result["implementation"] = implementation
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def read_many(solution: str | Path, requests: list[dict]) -> list[dict]:
    """Read many saved objects after one incremental synchronization pass."""
    sync_info = sync(solution)
    return [read(solution, str(item["name"]), path=str(item.get("path") or ""),
                 member=str(item.get("method") or ""), area=str(item.get("area") or "all"),
                 _sync_info=sync_info)
            for item in requests]


def status(solution: str | Path) -> dict:
    sync_info = sync(solution)
    with _LOCK, _connection(solution) as db:
        count = int(db.execute("SELECT COUNT(*) FROM plc_source").fetchone()[0])
        members = int(db.execute("SELECT COUNT(*) FROM plc_source_member").fetchone()[0])
    return {**sync_info, "count": count, "member_count": members}


def catalog(solution: str | Path, *, query: str = "", kind: str = "",
            include_members: bool = True, limit: int = 200) -> dict:
    """Return a compact project map without returning any PLC source text."""
    started = time.perf_counter()
    sync_info = sync(solution)
    limit = min(500, max(1, int(limit)))
    needle = str(query or "").casefold()
    wanted_kind = str(kind or "").casefold()
    with _LOCK, _connection(solution) as db:
        roots = list(db.execute(
            "SELECT file, project, logical_path, name, kind FROM plc_source "
            "ORDER BY kind COLLATE NOCASE, name COLLATE NOCASE"
        ))
        member_rows = list(db.execute(
            "SELECT file, member FROM plc_source_member ORDER BY member COLLATE NOCASE"
        )) if include_members else []
    members_by_file: dict[str, list[str]] = {}
    for row in member_rows:
        members_by_file.setdefault(str(row["file"]), []).append(str(row["member"]))
    items: list[dict] = []
    for row in roots:
        members = members_by_file.get(str(row["file"]), [])
        haystack = " ".join((str(row["name"]), str(row["logical_path"]), *members)).casefold()
        if needle and needle not in haystack:
            continue
        if wanted_kind and str(row["kind"]).casefold() != wanted_kind:
            continue
        items.append({"name": row["name"], "kind": row["kind"], "project": row["project"],
                      "path": row["logical_path"], "source_path": row["logical_path"],
                      "path_kind": "saved_source", "tree_path": "",
                      "tree_path_available": False,
                      "members": members if include_members else []})
        if len(items) >= limit:
            break
    return {"status": "catalog", "query": query, "kind": kind, "count": len(items),
            "truncated": len(items) >= limit, "limit": limit, "items": items,
            "source": "disk_index", "index_sync": sync_info,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}


def search(solution: str | Path, pattern: str, *, regex: bool = False,
           case_sensitive: bool = False, pou: str = "", path: str = "",
           max_results: int = 50) -> dict:
    """Search indexed saved source and return the same line-level contract as COM."""
    started = time.perf_counter()
    if not pattern:
        raise ValueError("pattern is required")
    limit = min(500, max(1, int(max_results)))
    sync_info = sync(solution)
    with _LOCK, _connection(solution) as db:
        roots = list(db.execute("SELECT * FROM plc_source" +
            (" WHERE name = ? COLLATE NOCASE" if pou else ""), (pou,) if pou else ()))
        members = list(db.execute("""
            SELECT p.*, m.member, m.declaration AS member_declaration,
                   m.implementation AS member_implementation
            FROM plc_source_member m JOIN plc_source p ON p.file=m.file
        """ + (" WHERE p.name = ? COLLATE NOCASE" if pou else ""), (pou,) if pou else ()))
    if path:
        normalized = path.replace("/", "^").replace("\\", "^").casefold()
        roots = [row for row in roots if normalized.endswith(str(row["logical_path"]).casefold())
                 or str(row["logical_path"]).casefold() in normalized]
        members = [row for row in members if normalized.endswith(str(row["logical_path"]).casefold())
                   or str(row["logical_path"]).casefold() in normalized]
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern, flags) if regex else None
    needle = pattern if case_sensitive else pattern.casefold()

    def hit(line: str) -> bool:
        return bool(matcher.search(line)) if matcher else needle in (line if case_sensitive else line.casefold())

    matches: list[dict] = []
    for row in [*roots, *members]:
        member = str(row["member"] or "") if "member" in row.keys() else ""
        target = f"{row['name']}.{member}" if member else str(row["name"])
        areas = (("declaration", row["member_declaration"] if member else row["declaration"]),
                 ("implementation", row["member_implementation"] if member else row["implementation"]))
        for area, text in areas:
            for line_no, line in enumerate(str(text or "").splitlines(), 1):
                if hit(line):
                    matches.append({"pou": target, "path": row["logical_path"],
                                    "area": area, "line": line_no, "text": line.strip()[:500]})
                    if len(matches) >= limit:
                        return {"pattern": pattern, "pou": pou, "path": path,
                                "count": len(matches), "truncated": True,
                                "max_results": limit, "matches": matches,
                                "source": "disk_index", "index_sync": sync_info,
                                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    return {"pattern": pattern, "pou": pou, "path": path, "count": len(matches),
            "truncated": False, "max_results": limit, "matches": matches,
            "source": "disk_index", "index_sync": sync_info,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
