"""Small in-memory cache fed by the XAE extension for recently opened PLC code."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar


def normalized(value):
    return str(value or "").replace("/", "\\").casefold()


_scope = ContextVar("plc_cache_scope", default=(0, ""))


def scoped_solution() -> str:
    return _scope.get()[1]


@contextmanager
def cache_scope(pid: int, solution: str):
    token = _scope.set((int(pid or 0), normalized(solution)))
    try:
        yield
    finally:
        _scope.reset(token)


class PlcSourceCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[tuple, dict] = {}
        self.revision = 0

    def invalidate(self):
        with self._lock:
            self._items.clear()
            self.revision += 1

    def invalidate_document(self, pid: int, solution: str, path: str, member: str = ""):
        """An editor notification invalidates only its own scoped member."""
        key = (int(pid), normalized(solution), normalized(path), normalized(member))
        with self._lock:
            if self._items.pop(key, None) is not None:
                self.revision += 1

    def put(self, payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        content = str(payload.get("content") or "")
        member = str(payload.get("member") or "").strip()
        if not path or Path(path).suffix.casefold() not in {".tcpou", ".tcgvl", ".tcdut", ".tcio"}:
            raise ValueError("无效 PLC 缓存更新")
        item = {
            "name": Path(path).stem, "source_file": path, "member": member,
            "content": content.replace("\r\n", "\n").replace("\r", "\n"),
            "saved": bool(payload.get("saved", True)), "updated_at": time.time(),
            "pid": int(payload.get("xae_pid") or 0), "solution": normalized(payload.get("solution")),
            "tree_path": str(payload.get("tree_path") or ""),
            "areas": {key: str(payload[key]).replace("\r\n", "\n").replace("\r", "\n")
                      for key in ("declaration", "implementation") if key in payload},
        }
        try:
            stat = Path(path).stat()
            item["disk_stamp"] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            item["disk_stamp"] = None
        if item["areas"]:
            item["content"] = repr(sorted(item["areas"].items()))
        item["hash"] = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        with self._lock:
            self._items[(item["pid"], item["solution"], normalized(path), normalized(member))] = item
            self.revision += 1
        return {key: item[key] for key in ("name", "source_file", "member", "saved", "updated_at", "hash")}

    def get(self, name: str, member: str = "", area: str = "all", *, path: str = "") -> dict | None:
        pid, solution = _scope.get()
        if not pid or not solution:
            return None
        requested = str(name or "").casefold()
        wanted_member = str(member or "").casefold()
        with self._lock:
            matches = [item for item in self._items.values()
                       if item["name"].casefold() == requested and item["member"].casefold() == wanted_member
                       and item["pid"] == pid and item["solution"] == solution
                       and (not path or normalized(path) in
                            {normalized(item["source_file"]), normalized(item["tree_path"])})]
        if len(matches) != 1:
            return None
        item = dict(matches[0])
        wanted = ("declaration", "implementation") if area == "all" else (area,)
        if time.time() - item["updated_at"] > 60 or not all(key in item["areas"] for key in wanted):
            return None
        try:
            stat = Path(item["source_file"]).stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != item["disk_stamp"]:
            return None
        tree_path = str(item.get("tree_path") or "")
        result = {"name": item["name"], "method": item["member"],
                  "source_file": item["source_file"], "source_path": item["source_file"],
                  "path": tree_path, "tree_path": tree_path,
                  "path_kind": "com_tree" if tree_path else "unknown",
                  "tree_path_available": bool(tree_path),
                  "source": "xae_cache", "live_xae": True, "authoritative": False,
                  "cache_saved": item["saved"], "cache_updated_at": item["updated_at"],
                  "hashes": {key: hashlib.sha256(item["areas"][key].encode("utf-8")).hexdigest()
                             for key in wanted}}
        result.update({key: item["areas"][key] for key in wanted})
        return result

    def snapshot(self) -> dict:
        with self._lock:
            items = [
                {key: item[key] for key in ("name", "source_file", "member", "saved", "updated_at", "hash")}
                | {"chars": len(item["content"])}
                for item in self._items.values()
            ]
        return {"count": len(items), "items": items}


CACHE = PlcSourceCache()
