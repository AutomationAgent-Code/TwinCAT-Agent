"""Project-local PLC snapshots and bounded semantic code diffs."""

from __future__ import annotations

import difflib
import gzip
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from tc_template._ps_bridge import com_build, com_code_inventory, ps_com


SCHEMA_VERSION = 1
SNAPSHOT_DIR = ".TwinCATAgent/snapshots"
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")
_REPARSE_POINT = 0x400
MAX_SNAPSHOT_PAGE = 100
MAX_SNAPSHOT_OBJECT_PAGE = 200
MAX_SNAPSHOT_CONTENT_CHARS = 50000


def _inventory(*, include_code: bool, paths: list[str] | None = None) -> dict:
    data = com_code_inventory(include_code=include_code, paths=list(paths or []))
    if not isinstance(data, dict) or not data.get("solution"):
        raise RuntimeError("XAE 当前没有可用于版本比较的已打开解决方案。")
    return data


def _solution_path(solution: str) -> Path:
    raw = str(solution or "").strip()
    if not raw:
        raise RuntimeError("XAE 当前没有打开解决方案。")
    sln = Path(raw).expanduser()
    if not sln.is_absolute():
        raise RuntimeError("当前解决方案路径必须是绝对路径。")
    sln = sln.resolve(strict=False)
    if sln.suffix.lower() != ".sln":
        raise RuntimeError(f"当前解决方案路径无效：{solution}")
    if not sln.is_file():
        raise RuntimeError(f"当前解决方案文件不存在：{solution}")
    return sln


def _snapshot_root(solution: str) -> Path:
    sln = _solution_path(solution)
    return sln.parent / SNAPSHOT_DIR


def _safe_label(name: str) -> str:
    label = _SAFE_NAME.sub("-", str(name or "manual").strip()).strip("-._")
    return (label or "manual")[:48]


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows junctions before reading a snapshot."""
    try:
        if path.is_symlink():
            return True
        attrs = int(getattr(path.stat(), "st_file_attributes", 0))
        return bool(attrs & _REPARSE_POINT)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"无法检查快照路径：{path.name} ({exc})") from exc


def _assert_safe_snapshot_root(root: Path) -> Path:
    raw_root = root
    if _is_reparse_point(raw_root.parent) or _is_reparse_point(raw_root):
        raise RuntimeError("快照目录包含符号链接或 junction，已拒绝读取。")
    root = raw_root.resolve(strict=False)
    if not root.exists():
        return root
    if not root.is_dir():
        raise RuntimeError("PLC 快照路径不是目录。")
    return root


def _snapshot_candidates(root: Path) -> list[Path]:
    root = _assert_safe_snapshot_root(root)
    if not root.is_dir():
        return []
    try:
        entries = [p for p in root.iterdir() if p.name.lower().endswith(".json.gz")]
    except PermissionError as exc:
        raise RuntimeError("没有权限读取当前工程的 PLC 快照目录。") from exc
    except OSError as exc:
        raise RuntimeError(f"读取 PLC 快照目录失败：{exc}") from exc
    return sorted(entries, key=lambda p: p.lstat().st_mtime_ns, reverse=True)


def _snapshot_files(root: Path) -> list[Path]:
    return [p for p in _snapshot_candidates(root)
            if p.is_file() and not _is_reparse_point(p)]


def _safe_snapshot_file(path: Path, root: Path) -> Path:
    root = _assert_safe_snapshot_root(root)
    if path.parent.resolve(strict=False) != root.resolve(strict=False):
        raise RuntimeError("快照路径越出当前工程快照目录。")
    if _is_reparse_point(path) or not path.is_file():
        raise RuntimeError("快照文件不是当前工程目录内的普通文件。")
    return path


def _resolve_snapshot_file(solution: str, reference: str = "latest") -> tuple[Path, Path]:
    root = _snapshot_root(solution)
    files = _snapshot_candidates(root)
    if not files:
        raise RuntimeError("当前项目还没有快照，请先执行 plc_snapshot。")
    ref = str(reference or "latest").strip()
    if any(separator in ref for separator in ("/", "\\")) or Path(ref).name != ref:
        raise RuntimeError("快照引用只能是当前工程快照目录中的文件名或标签。")
    candidates = files
    if ref.lower() != "latest":
        candidates = [p for p in files if p.name == ref]
        if not candidates:
            candidates = [p for p in files if f"-{_safe_label(ref)}.json.gz" in p.name]
    if not candidates:
        raise RuntimeError(f"找不到快照：{reference}")
    return _safe_snapshot_file(candidates[0], root), root


def _read_verified_snapshot(solution: str, reference: str = "latest") -> tuple[Path, dict, Path]:
    path, root = _resolve_snapshot_file(solution, reference)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"快照损坏或无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"快照损坏或无法读取：{path.name}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"不支持的快照格式：{path.name}")
    try:
        payload_solution = Path(payload.get("solution", "")).expanduser().resolve()
        current_solution = _solution_path(solution)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"快照缺少有效工程归属：{path.name}") from exc
    if payload_solution != current_solution:
        raise RuntimeError("快照不属于当前解决方案。")
    return path, payload, root


def _load_snapshot(solution: str, reference: str = "latest") -> tuple[Path, dict]:
    path, payload, _ = _read_verified_snapshot(solution, reference)
    return path, payload


def create_snapshot(name: str = "manual") -> dict:
    inventory = _inventory(include_code=True)
    root = _snapshot_root(inventory["solution"])
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    label = _safe_label(name)
    path = root / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{label}.json.gz"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": str(name or "manual"),
        "created_at": now.isoformat(),
        **inventory,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "status": "created",
        "name": payload["name"],
        "file": str(path),
        "solution": inventory["solution"],
        "object_count": inventory.get("object_count", 0),
        "compressed_bytes": path.stat().st_size,
        "created_at": payload["created_at"],
    }


def list_snapshots(limit: int = 20) -> dict:
    info = ps_com("project-info")
    solution = str((info or {}).get("solution") or "")
    if not solution:
        raise RuntimeError("XAE 当前没有打开解决方案。")
    return list_snapshots_for_solution(solution, limit=limit)


def _area_summary(area: object) -> dict:
    data = area if isinstance(area, dict) else {}
    text = data.get("text")
    return {
        "available": isinstance(text, str),
        "hash": str(data.get("hash") or ""),
        "lines": max(0, int(data.get("lines") or 0)),
        "chars": max(0, int(data.get("chars") or (len(text) if isinstance(text, str) else 0))),
    }


def _snapshot_metadata(path: Path, payload: dict) -> dict:
    objects = [item for item in (payload.get("objects") or []) if isinstance(item, dict)]
    kind_counts: dict[str, int] = {}
    member_count = 0
    code_chars = 0
    for obj in objects:
        kind = str(obj.get("kind") or obj.get("folder") or "unknown")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        member_count += len([m for m in (obj.get("members") or []) if isinstance(m, dict)])
        for area_name in ("declaration", "implementation"):
            code_chars += _area_summary(obj.get(area_name)).get("chars", 0)
        for member in obj.get("members") or []:
            if not isinstance(member, dict):
                continue
            for area_name in ("declaration", "implementation"):
                code_chars += _area_summary(member.get(area_name)).get("chars", 0)
    try:
        compressed_bytes = path.stat().st_size
    except OSError:
        compressed_bytes = 0
    return {
        "name": str(payload.get("name") or path.stem.removesuffix(".json")),
        "file": path.name,
        "created_at": str(payload.get("created_at") or ""),
        "object_count": len(objects),
        "member_count": member_count,
        "kind_counts": kind_counts,
        "code_chars": code_chars,
        "compressed_bytes": compressed_bytes,
        "schema_version": payload.get("schema_version"),
        "category": "PLC 代码快照",
        "kind": "plc_code",
    }


def _snapshot_matches(item: dict, query: str) -> bool:
    query = str(query or "").strip().casefold()
    if not query:
        return True
    return any(query in str(item.get(key) or "").casefold()
               for key in ("name", "file", "category"))


def list_snapshots_for_solution(
    solution: str, *, limit: int = 20, offset: int = 0,
    query: str = "", sort: str = "created_desc",
) -> dict:
    """Return a bounded, read-only catalog for one already-bound solution."""
    solution_path = _solution_path(solution)
    root = _assert_safe_snapshot_root(_snapshot_root(str(solution_path)))
    page_limit = max(1, min(int(limit or 20), MAX_SNAPSHOT_PAGE))
    page_offset = max(0, int(offset or 0))
    items: list[dict] = []
    for path in _snapshot_candidates(root):
        item: dict
        try:
            safe_path = _safe_snapshot_file(path, root)
            with gzip.open(safe_path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise RuntimeError("快照内容不是对象")
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError(f"不支持的快照格式：{path.name}")
            payload_solution = Path(payload.get("solution", "")).expanduser().resolve()
            if payload_solution != solution_path:
                raise RuntimeError("快照不属于当前解决方案")
            item = _snapshot_metadata(safe_path, payload)
        except (OSError, EOFError, UnicodeError, ValueError, RuntimeError, gzip.BadGzipFile) as exc:
            item = {
                "file": path.name,
                "name": path.name,
                "category": "PLC 代码快照",
                "kind": "plc_code",
                "error": "快照损坏、越界或无法读取",
                "error_detail": str(exc),
            }
        if _snapshot_matches(item, query):
            items.append(item)

    valid_items = [item for item in items if not item.get("error")]
    error_items = [item for item in items if item.get("error")]
    if sort == "name_asc":
        valid_items.sort(key=lambda item: str(item.get("name") or "").casefold())
    elif sort == "size_desc":
        valid_items.sort(key=lambda item: -int(item.get("compressed_bytes") or 0))
    elif sort == "created_asc":
        valid_items.sort(key=lambda item: str(item.get("created_at") or ""))
    else:
        valid_items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    items = valid_items + error_items
    total = len(items)
    page = items[page_offset:page_offset + page_limit]
    return {
        "solution": str(solution_path),
        "snapshot_dir": str(root),
        "directory_exists": root.is_dir(),
        "category": "PLC 代码快照",
        "kind": "plc_code",
        "count": len(page),
        "total": total,
        "offset": page_offset,
        "limit": page_limit,
        "has_more": page_offset + len(page) < total,
        "snapshots": page,
    }


def _object_summary(obj: dict) -> dict:
    members = []
    for member in obj.get("members") or []:
        if not isinstance(member, dict):
            continue
        members.append({
            "name": str(member.get("name") or ""),
            "path": str(member.get("path") or ""),
            "kind": str(member.get("kind") or "member"),
            "declaration": _area_summary(member.get("declaration")),
            "implementation": _area_summary(member.get("implementation")),
        })
    return {
        "name": str(obj.get("name") or ""),
        "path": str(obj.get("path") or ""),
        "kind": str(obj.get("kind") or ""),
        "folder": str(obj.get("folder") or ""),
        "item_type": obj.get("itemType"),
        "declaration": _area_summary(obj.get("declaration")),
        "implementation": _area_summary(obj.get("implementation")),
        "members": members,
    }


def snapshot_detail(
    solution: str, reference: str = "latest", *, offset: int = 0,
    limit: int = 100, query: str = "",
) -> dict:
    path, payload, root = _read_verified_snapshot(solution, reference)
    metadata = _snapshot_metadata(path, payload)
    objects = [_object_summary(item) for item in (payload.get("objects") or [])
               if isinstance(item, dict)]
    query = str(query or "").strip().casefold()
    if query:
        objects = [item for item in objects if query in str(item.get("name") or "").casefold()
                   or query in str(item.get("path") or "").casefold()
                   or query in str(item.get("kind") or "").casefold()]
    page_limit = max(1, min(int(limit or 100), MAX_SNAPSHOT_OBJECT_PAGE))
    page_offset = max(0, int(offset or 0))
    page = objects[page_offset:page_offset + page_limit]
    return {
        **metadata,
        "snapshot": path.name,
        "solution": str(_solution_path(solution)),
        "snapshot_dir": str(root),
        "objects": page,
        "object_total": len(objects),
        "object_offset": page_offset,
        "object_limit": page_limit,
        "object_has_more": page_offset + len(page) < len(objects),
    }


def snapshot_content(
    solution: str, reference: str, object_path: str, *,
    member_path: str = "", area: str = "declaration",
    offset: int = 0, max_chars: int = 12000,
) -> dict:
    if area not in {"declaration", "implementation"}:
        raise ValueError("只支持读取 declaration 或 implementation。")
    path, payload, root = _read_verified_snapshot(solution, reference)
    object_key = str(object_path or "")
    target = next((obj for obj in (payload.get("objects") or [])
                   if isinstance(obj, dict) and str(obj.get("path") or "") == object_key), None)
    if target is None:
        raise RuntimeError("快照中找不到指定 PLC 对象。")
    target_label = object_key
    if member_path:
        target = next((member for member in (target.get("members") or [])
                       if isinstance(member, dict) and str(member.get("path") or "") == str(member_path)), None)
        target_label = str(member_path)
        if target is None:
            raise RuntimeError("快照中找不到指定成员。")
    area_data = target.get(area) if isinstance(target, dict) else None
    if not isinstance(area_data, dict) or not isinstance(area_data.get("text"), str):
        raise RuntimeError(f"该对象没有可读取的{area}内容。")
    text = area_data["text"]
    start = max(0, int(offset or 0))
    cap = max(1000, min(int(max_chars or 12000), MAX_SNAPSHOT_CONTENT_CHARS))
    content = text[start:start + cap]
    next_offset = start + len(content)
    return {
        "solution": str(_solution_path(solution)),
        "snapshot_dir": str(root),
        "snapshot": path.name,
        "object_path": object_key,
        "member_path": str(member_path or ""),
        "target": target_label,
        "area": area,
        "content": content,
        "total_chars": len(text),
        "offset": start,
        "next_offset": next_offset if next_offset < len(text) else None,
        "truncated": next_offset < len(text),
        "hash": str(area_data.get("hash") or ""),
        "lines": max(0, int(area_data.get("lines") or len(text.splitlines()))),
    }


def _area_hash(area: dict | None) -> str:
    return str((area or {}).get("hash") or "")


def _object_fingerprint(obj: dict) -> tuple:
    members = tuple(sorted(
        (
            str(m.get("path") or ""),
            _area_hash(m.get("declaration")),
            _area_hash(m.get("implementation")),
        )
        for m in obj.get("members") or []
    ))
    return (
        int(obj.get("itemType") or 0),
        _area_hash(obj.get("declaration")),
        _area_hash(obj.get("implementation")),
        members,
    )


def _object_map(inventory: dict) -> dict[str, dict]:
    return {
        str(obj.get("path")): obj
        for obj in inventory.get("objects") or []
        if obj.get("path")
    }


def _changed_areas(before: dict | None, after: dict | None) -> list[str]:
    if before is None or after is None:
        return ["object"]
    areas = []
    for area in ("declaration", "implementation"):
        if _area_hash(before.get(area)) != _area_hash(after.get(area)):
            areas.append(area)
    old_members = {str(m.get("path")): m for m in before.get("members") or []}
    new_members = {str(m.get("path")): m for m in after.get("members") or []}
    for path in sorted(set(old_members) | set(new_members)):
        old, new = old_members.get(path), new_members.get(path)
        if old is None or new is None:
            areas.append(f"member:{path.rsplit('^', 1)[-1]}")
            continue
        for area in ("declaration", "implementation"):
            if _area_hash(old.get(area)) != _area_hash(new.get(area)):
                areas.append(f"member:{path.rsplit('^', 1)[-1]}.{area}")
    return areas


def _compare(snapshot: dict, current: dict, name: str = "") -> list[dict]:
    old_map, new_map = _object_map(snapshot), _object_map(current)
    query = str(name or "").casefold()
    changes = []
    for path in sorted(set(old_map) | set(new_map)):
        old, new = old_map.get(path), new_map.get(path)
        obj = new or old or {}
        if query and query not in str(obj.get("name") or "").casefold() and query not in path.casefold():
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        elif _object_fingerprint(old) != _object_fingerprint(new):
            status = "modified"
        else:
            continue
        changes.append({
            "status": status,
            "name": obj.get("name", ""),
            "kind": obj.get("kind", ""),
            "path": path,
            "areas": _changed_areas(old, new),
        })
    return changes


def changed(snapshot: str = "latest", name: str = "", limit: int = 100) -> dict:
    current = _inventory(include_code=False)
    snap_path, baseline = _load_snapshot(current["solution"], snapshot)
    all_changes = _compare(baseline, current, name=name)
    cap = max(1, min(int(limit), 500))
    counts = {status: sum(c["status"] == status for c in all_changes)
              for status in ("added", "modified", "removed")}
    return {
        "snapshot": snap_path.name,
        "solution": current["solution"],
        "changed": bool(all_changes),
        "counts": counts,
        "total": len(all_changes),
        "truncated": len(all_changes) > cap,
        "changes": all_changes[:cap],
    }


def _components(obj: dict | None) -> dict[str, str]:
    if not obj:
        return {}
    out = {}
    base = str(obj.get("path") or obj.get("name") or "object")
    for area in ("declaration", "implementation"):
        out[f"{base}::{area}"] = str((obj.get(area) or {}).get("text") or "")
    for member in obj.get("members") or []:
        member_path = str(member.get("path") or member.get("name") or "member")
        for area in ("declaration", "implementation"):
            out[f"{member_path}::{area}"] = str((member.get(area) or {}).get("text") or "")
    return out


def diff(snapshot: str = "latest", name: str = "", context: int = 3,
         max_hunks: int = 50, max_chars: int = 12000) -> dict:
    hashes = _inventory(include_code=False)
    snap_path, baseline = _load_snapshot(hashes["solution"], snapshot)
    changes = _compare(baseline, hashes, name=name)
    paths = [c["path"] for c in changes if c["status"] != "removed"]
    current_code = _inventory(include_code=True, paths=paths) if paths else {
        "objects": [], "solution": hashes["solution"]}
    old_map, new_map = _object_map(baseline), _object_map(current_code)
    context = max(0, min(int(context), 20))
    max_hunks = max(1, min(int(max_hunks), 200))
    max_chars = max(1000, min(int(max_chars), 50000))
    hunks = []
    used = 0
    for change in changes:
        path = change["path"]
        old_components = _components(old_map.get(path))
        new_components = _components(new_map.get(path))
        for key in sorted(set(old_components) | set(new_components)):
            old_text, new_text = old_components.get(key, ""), new_components.get(key, "")
            if old_text == new_text:
                continue
            lines = list(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f"snapshot:{key}", tofile=f"current:{key}",
                lineterm="", n=context,
            ))
            text = "\n".join(lines)
            if not text:
                continue
            remaining = max_chars - used
            if remaining <= 0 or len(hunks) >= max_hunks:
                break
            clipped = len(text) > remaining
            if clipped:
                text = text[:remaining] + "\n…diff truncated…"
            hunks.append({"object": change["name"], "path": path,
                          "component": key.rsplit("::", 1)[-1], "diff": text})
            used += len(text)
            if clipped:
                break
        if used >= max_chars or len(hunks) >= max_hunks:
            break
    return {
        "snapshot": snap_path.name,
        "solution": hashes["solution"],
        "change_count": len(changes),
        "hunk_count": len(hunks),
        "truncated": (used >= max_chars or len(hunks) >= max_hunks or len(changes) > 200),
        "change_total": len(changes),
        "changes": changes[:200],
        "hunks": hunks,
    }


def _structural_changes(before: dict, after: dict) -> list[dict]:
    """Return changes that cannot be restored safely from a code-only snapshot."""
    old_map, new_map = _object_map(before), _object_map(after)
    changes: list[dict] = []
    for path in sorted(set(old_map) ^ set(new_map)):
        obj = old_map.get(path) or new_map.get(path) or {}
        changes.append({
            "kind": "object", "path": path, "name": obj.get("name", ""),
            "status": "missing_current" if path in old_map else "added_current",
        })
    for path in sorted(set(old_map) & set(new_map)):
        old_members = {str(m.get("path")): m for m in old_map[path].get("members") or []}
        new_members = {str(m.get("path")): m for m in new_map[path].get("members") or []}
        for member_path in sorted(set(old_members) ^ set(new_members)):
            member = old_members.get(member_path) or new_members.get(member_path) or {}
            changes.append({
                "kind": "member", "path": member_path,
                "name": member.get("name", ""),
                "status": "missing_current" if member_path in old_members else "added_current",
            })
    return changes


def _write_snapshot_code(inventory: dict, compared_to: dict) -> int:
    """Write only code areas whose hashes differ from the comparison inventory."""
    writes = 0
    other_objects = _object_map(compared_to)
    for obj in inventory.get("objects") or []:
        name, path = str(obj.get("name") or ""), str(obj.get("path") or "")
        other = other_objects.get(path) or {}
        for area in ("declaration", "implementation"):
            if _area_hash(obj.get(area)) == _area_hash(other.get(area)):
                continue
            ps_com("write-pou", name=name, code=str((obj.get(area) or {}).get("text") or ""),
                   area=area, path=path, method="")
            writes += 1
        other_members = {str(m.get("path")): m for m in other.get("members") or []}
        for member in obj.get("members") or []:
            member_name = str(member.get("name") or "")
            other_member = other_members.get(str(member.get("path") or "")) or {}
            for area in ("declaration", "implementation"):
                if _area_hash(member.get(area)) == _area_hash(other_member.get(area)):
                    continue
                ps_com("write-pou", name=name,
                       code=str((member.get(area) or {}).get("text") or ""),
                       area=area, path=path, method=member_name)
                writes += 1
    return writes


def restore_snapshot(snapshot: str = "latest", *, apply: bool = False) -> dict:
    """Preview or transactionally restore code from a project-local snapshot.

    Snapshots intentionally contain code rather than enough Automation
    Interface metadata to recreate arbitrary folders and object types.  A
    structural mismatch therefore blocks restoration instead of guessing.
    """
    current = _inventory(include_code=True)
    snap_path, baseline = _load_snapshot(current["solution"], snapshot)
    changes = _compare(baseline, current)
    structural = _structural_changes(baseline, current)
    result = {
        "snapshot": snap_path.name,
        "solution": current["solution"],
        "change_count": len(changes),
        "changes": changes[:200],
        "structural_changes": structural[:100],
    }
    if structural:
        return {**result, "status": "blocked", "reason":
                "快照与当前项目存在对象或成员结构差异；代码快照不会猜测重建 TwinCAT 对象。"}
    if not apply:
        return {**result, "status": "preview", "would_write": bool(changes)}
    if not changes:
        return {**result, "status": "unchanged", "writes": 0}

    backup = create_snapshot("pre-restore")
    try:
        writes = _write_snapshot_code(baseline, current)
        build = com_build(always_read_errors=True)
        failed = int(build.get("failedProjects") or 0) > 0 or int(build.get("errorCount") or 0) > 0
        if failed:
            rollback_writes = _write_snapshot_code(current, baseline)
            rollback_build = com_build(always_read_errors=True)
            return {**result, "status": "rolled_back", "writes": writes,
                    "backup": backup, "build": build,
                    "rollback_writes": rollback_writes,
                    "rollback_build": rollback_build,
                    "reason": "恢复后的编译门禁未通过，已自动写回操作前代码。"}
        return {**result, "status": "restored", "writes": writes,
                "backup": backup, "build": build}
    except Exception as exc:
        rollback_error = ""
        try:
            _write_snapshot_code(current, baseline)
        except Exception as rollback_exc:
            rollback_error = str(rollback_exc)
        return {**result, "status": "rolled_back", "backup": backup,
                "error": str(exc), "rollback_error": rollback_error,
                "reason": "恢复过程中发生异常，已尝试写回操作前代码。"}
