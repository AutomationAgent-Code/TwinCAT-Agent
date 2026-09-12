"""Fast, read-only TwinCAT PLC source access from project XML files.

The disk reader is deliberately separate from the XAE COM bridge.  It is used
for low-latency context loading only; mutations and authoritative live reads
remain COM operations.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET


_SOURCE_SUFFIXES = {".tcpou", ".tcgvl", ".tcdut", ".tcio"}
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, tuple[tuple[int, int], list["SourceEntry"]]] = {}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext())


def _normalize_code(value: str) -> str:
    """Match the COM bridge's line slicing semantics and ignore XML EOL trivia."""
    return "\n".join(str(value or "").splitlines())


def _child(element: ET.Element, name: str) -> ET.Element | None:
    wanted = name.casefold()
    return next((item for item in element if _local_name(item.tag).casefold() == wanted), None)


def _code(element: ET.Element) -> tuple[str, str]:
    declaration = _text(_child(element, "Declaration"))
    implementation = ""
    impl = _child(element, "Implementation")
    if impl is not None:
        st = next((item for item in impl.iter() if _local_name(item.tag) == "ST"), None)
        implementation = _text(st if st is not None else impl)
    return _normalize_code(declaration), _normalize_code(implementation)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceEntry:
    project: str
    project_file: Path
    file: Path
    include: str
    name: str
    kind: str

    @property
    def logical_path(self) -> str:
        include_path = self.include.replace("\\", "^").replace("/", "^")
        return f"{self.project}^{include_path}"


def _project_name(root: ET.Element, fallback: str) -> str:
    for element in root.iter():
        if _local_name(element.tag) == "Name" and (element.text or "").strip():
            return (element.text or "").strip()
    return fallback


def _index_project(project_file: Path) -> list[SourceEntry]:
    stat = project_file.stat()
    key = str(project_file.resolve()).casefold()
    signature = (stat.st_mtime_ns, stat.st_size)
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1]

    root = ET.parse(project_file).getroot()
    project = _project_name(root, project_file.stem)
    entries: list[SourceEntry] = []
    for element in root.iter():
        if _local_name(element.tag) != "Compile":
            continue
        include = str(element.attrib.get("Include") or "").strip()
        decoded_include = unquote(include)
        file = (project_file.parent / Path(decoded_include.replace("\\", "/"))).resolve()
        if file.suffix.casefold() not in _SOURCE_SUFFIXES:
            continue
        entries.append(SourceEntry(
            project=project, project_file=project_file.resolve(), file=file,
            include=include, name=file.stem, kind=file.suffix[3:].upper(),
        ))
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = (signature, entries)
    return entries


def discover_projects(solution: str | Path) -> list[Path]:
    """Follow saved project references; a directory explicitly selects offline scan."""
    solution_path = Path(solution).resolve()
    if solution_path.is_dir():
        excluded = {"_boot", ".git", ".codex", ".twincatagent", ".updates", "bin", "obj"}
        return sorted((path for path in solution_path.rglob("*.plcproj")
                       if not any(part.casefold() in excluded for part in path.parts)),
                      key=lambda path: str(path).casefold())
    found, visited = set(), set()

    def visit(path):
        path = path.resolve()
        key = str(path).casefold()
        if key in visited:
            return
        visited.add(key)
        suffix = path.suffix.casefold()
        if suffix == ".plcproj":
            if not path.is_file():
                raise FileNotFoundError(f"Referenced PLC project is missing: {path}")
            found.add(path)
            return
        if suffix == ".sln":
            text = path.read_text(encoding="utf-8-sig")
            references = re.findall(r'^Project\([^\n]+?\)\s*=\s*"[^"]*",\s*"([^"]+)"', text, re.M)
        elif suffix in {".tsproj", ".xti"}:
            root = ET.parse(path).getroot()
            references = [value for element in root.iter() for name, value in element.attrib.items()
                          if name in {"PrjFilePath", "File", "Include"}]
        else:
            raise ValueError(f"Unsupported project source: {path}")
        for reference in references:
            target = path.parent / unquote(reference).replace("\\", "/")
            if target.suffix.casefold() in {".tsproj", ".plcproj", ".xti"}:
                visit(target)

    visit(solution_path)
    return sorted(found, key=lambda path: str(path).casefold())


def source_index(solution: str | Path) -> list[SourceEntry]:
    result: list[SourceEntry] = []
    for project in discover_projects(solution):
        try:
            result.extend(_index_project(project))
        except (OSError, ET.ParseError):
            continue
    return result


def _select_entry(entries: list[SourceEntry], name: str, path: str = "") -> SourceEntry:
    matches = [item for item in entries if item.name.casefold() == name.casefold()]
    if path:
        normalized = path.replace("/", "^").replace("\\", "^").casefold()
        narrowed = [item for item in matches if _saved_source_path_matches(
            normalized, item.logical_path
        )]
        if not narrowed:
            choices = ", ".join(item.logical_path for item in matches[:8])
            raise ValueError(
                "requested path is not a saved-source index path for this PLC object; "
                "use plc_find/live=true for a COM tree path"
                + (f" (saved paths: {choices})" if choices else "")
            )
        matches = narrowed
    if not matches:
        raise FileNotFoundError(f"PLC source object not found on disk: {name}")
    if len(matches) > 1:
        choices = ", ".join(item.logical_path for item in matches[:8])
        raise ValueError(f"PLC object name is ambiguous: {name}; use path ({choices})")
    return matches[0]


def _saved_source_path_matches(requested: str, logical_path: str) -> bool:
    wanted = str(logical_path or "").replace("/", "^").replace("\\", "^").casefold()
    value = str(requested or "").replace("/", "^").replace("\\", "^").casefold()
    return value == wanted or value.endswith("^" + wanted)


def _member(root_object: ET.Element, member_path: str) -> ET.Element:
    current = root_object
    for part in (item for item in member_path.split(".") if item):
        wanted = part.casefold()
        descendants = [item for item in current.iter() if item is not current]
        # PLC members carry a Name attribute.  Never treat structural XML tags
        # such as <Implementation> as a Method named "Implementation".
        found = next((item for item in descendants
                      if str(item.attrib.get("Name") or "").casefold() == wanted), None)
        if found is None and wanted in {"get", "set"}:
            found = next((item for item in descendants
                          if not item.attrib.get("Name")
                          and _local_name(item.tag).casefold() == wanted), None)
        if found is None:
            raise FileNotFoundError(f"PLC member not found on disk: {member_path}")
        current = found
    return current


def read_source(solution: str | Path, name: str, *, path: str = "",
                member: str = "", area: str = "all") -> dict:
    started = time.perf_counter()
    entry = _select_entry(source_index(solution), name, path)
    return _read_entry(entry, name, member=member, area=area, started=started)


def read_source_file(file: str | Path, name: str, *, member: str = "",
                     area: str = "all") -> dict:
    """Read one known project file without scanning its solution or `.plcproj`."""
    started = time.perf_counter()
    path = Path(file).resolve()
    entry = SourceEntry(project="", project_file=path.parent, file=path,
                        include=path.name, name=name, kind=path.suffix[3:].upper())
    return _read_entry(entry, name, member=member, area=area, started=started)


def _read_entry(entry: SourceEntry, name: str, *, member: str, area: str,
                started: float) -> dict:
    stat = entry.file.stat()
    document = ET.parse(entry.file).getroot()
    object_element = next((item for item in document.iter()
                           if str(item.attrib.get("Name") or "").casefold() == name.casefold()), None)
    if object_element is None:
        object_element = document
    target = _member(object_element, member) if member else object_element
    declaration, implementation = _code(target)
    result = {
        "status": "read", "name": name, "method": member,
        "project": entry.project, "path": entry.logical_path,
        # A saved-source path is a project-file identity, not an Automation
        # Interface tree path.  Keep the legacy ``path`` field for callers
        # that render the saved index, but make the distinction explicit so a
        # model cannot safely reuse it for plc_write/plc_patch.
        "source_path": entry.logical_path, "path_kind": "saved_source",
        "tree_path": "", "tree_path_available": False,
        "file": str(entry.file), "project_file": str(entry.project_file),
        "kind": entry.kind, "source": "disk", "live_xae": False,
        "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
        "hashes": {
            "declaration": _sha256(declaration),
            "implementation": _sha256(implementation),
        },
    }
    if area in {"all", "declaration"}:
        result["declaration"] = declaration
    if area in {"all", "implementation"}:
        result["implementation"] = implementation
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result
