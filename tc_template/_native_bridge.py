"""PowerShell-free TwinCAT Automation Interface bridge.

The public command contract intentionally matches ``TcCom.ps1`` so the Agent
tool registry does not need to know which transport executes a command.  All
COM work happens inside one initialized worker-thread apartment and only plain
JSON-compatible data leaves this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _com, plc, tc_platform
from .hmi_creation import create_project as create_hmi_project
from ._com import (
    com_apartment,
    get_active_dte,
    retry_com_busy,
    target_process,
    _window_pid,
)


_FOLDERS = ("POUs", "DUTs", "GVLs", "Interfaces", "VISUs")
_MEMBER_TYPES = {608, 609, 610, 611, 612, 613, 614, 616, 654, 655}
_MEMBER_TYPE_NAMES = {
    608: "action", 609: "method", 610: "method", 611: "property",
    612: "property", 613: "propget", 614: "propset", 616: "transition",
    654: "propget", 655: "propset",
}
_MEMBER_TYPE_IDS = {
    "action": {608}, "method": {609, 610}, "property": {611, 612},
    "propget": {613, 654}, "propset": {614, 655},
    "transition": {616},
}
_KIND = {
    601: "folder", 602: "program", 603: "function", 604: "function_block",
    605: "enum", 606: "struct", 607: "union", 623: "alias", 615: "gvl",
    618: "interface", 619: "visualization",
}

# Only operations that can legitimately raise TwinCAT runtime/configuration
# confirmation dialogs are allowed to use Automation Interface SilentMode.
# The previous implementation enabled SilentMode for every tool call and never
# restored it, which also suppressed dialogs for later manual XAE operations.
_SCOPED_SILENT_COMMANDS = frozenset({
    "io-scan", "activate", "restart", "login", "logout", "start", "stop",
    "online", "config-mode", "run-mode",
})


def _begin_scoped_silent_mode(dte):
    """Temporarily enable XAE SilentMode and return state for restoration."""
    try:
        settings = dte.GetObject("TcAutomationSettings")
        previous = bool(settings.SilentMode)
        settings.SilentMode = True
        return settings, previous
    except Exception:
        return None


def _restore_scoped_silent_mode(state) -> None:
    if state is None:
        return
    settings, previous = state
    try:
        settings.SilentMode = previous
    except Exception:
        pass
_CATEGORY_BY_TYPE = {
    602: "POUs", 603: "POUs", 604: "POUs",
    605: "DUTs", 606: "DUTs", 607: "DUTs", 623: "DUTs",
    615: "GVLs", 618: "Interfaces", 619: "VISUs",
}


def _item_type(item) -> int:
    try:
        return int(item.ItemType)
    except Exception:
        return 0


def _name(item) -> str:
    try:
        return str(item.Name or "")
    except Exception:
        return ""


def _children(item) -> list:
    try:
        return list(item or [])
    except Exception:
        return []


def _member_payload(item, *, include_code: bool = False) -> dict[str, Any]:
    """Return a member tree without probing unsafe interface-accessor text.

    TwinCAT represents an interface property's ``Get``/``Set`` nodes as
    children of the property.  On some XAE versions those nodes report no
    ItemType and accessing their text can destabilize the IDE, but their names
    are safe and sufficient to expose the public interface contract.
    """
    item_type = _item_type(item)
    member = {"name": _name(item), "itemType": item_type}
    if include_code:
        member["declaration"] = _read_text(item, "declaration")
        member["implementation"] = _read_text(item, "implementation")
    children = []
    for child in _children(item):
        child_name = _name(child)
        if not child_name:
            continue
        child_type = _item_type(child)
        # The property accessor nodes can report ItemType 0 in XAE.  Retain
        # only the known accessor names in that case, so ordinary hidden UI
        # nodes do not leak into the public PLC structure.
        if child_type in _MEMBER_TYPES or (item_type == 612 and child_name in {"Get", "Set"}):
            children.append(_member_payload(child, include_code=False))
    if children:
        member["members"] = children
    return member


def _system_manager(dte):
    count = int(dte.Solution.Projects.Count)
    for index in range(1, count + 1):
        try:
            candidate = dte.Solution.Projects.Item(index).Object
            candidate.LookupTreeItem("TIPC")
            return candidate
        except Exception:
            continue
    raise RuntimeError("TwinCAT System Manager project not found in the current solution.")


def _plc_roots(sysman):
    """Yield every PLC project's tree path, project name and root node.

    TwinCAT doesn't require objects to live below folders literally named
    POUs/DUTs/GVLs.  Real projects often put mixed object types directly below
    numbered or localized folders, so callers must walk this root recursively
    and classify definitions by ItemType.
    """
    plc_root = sysman.LookupTreeItem("TIPC")
    for controller in _children(plc_root):
        try:
            nested = controller.NestedProject
        except Exception:
            nested = None
        if nested is not None:
            nested_name = _name(nested)
            yield f"TIPC^{_name(controller)}^{nested_name}", nested_name, nested


def _plc_root(sysman):
    """Return the first PLC project for operations that require a default."""
    for root in _plc_roots(sysman):
        return root
    raise RuntimeError("No PLC project found in the current solution.")


def _plc_base(sysman) -> tuple[str, str]:
    base, name, _ = _plc_root(sysman)
    return base, name


def _iter_objects(sysman, folders: tuple[str, ...] = _FOLDERS):
    """Iterate definitions across all PLC projects under TIPC."""
    requested = set(folders)

    def walk(parent, parent_path: str, depth: int):
        for item in _children(parent):
            item_name = _name(item)
            if not item_name:
                continue
            path = f"{parent_path}^{item_name}"
            item_type = _item_type(item)
            if item_type == 601:
                # Standard category folders are organizational containers, not
                # an extra logical nesting level. Custom folders remain visible
                # through path/parent/depth on every returned object.
                next_depth = depth if item_name in _FOLDERS else depth + 1
                yield from walk(item, path, next_depth)
                continue
            category = _CATEGORY_BY_TYPE.get(item_type)
            if category not in requested:
                continue
            yield {
                "item": item,
                "name": item_name,
                "folder": category,
                "itemType": item_type,
                "path": path,
                "parent": parent_path,
                "depth": depth,
            }

    for base, _, root in _plc_roots(sysman):
        yield from walk(root, base, 0)


def _find_object(sysman, name: str, path: str = ""):
    if path:
        bases = [base for base, _, _ in _plc_roots(sysman)]
        if not any(path.casefold().startswith((base + "^").casefold()) for base in bases):
            raise ValueError(f"Tree path is outside the current PLC project: {path}")
        try:
            item = sysman.LookupTreeItem(path)
        except Exception as exc:
            raise FileNotFoundError(f"Object '{name}' was not found at tree path '{path}'.") from exc
        if _name(item).casefold() != str(name).casefold():
            raise FileNotFoundError(f"Tree path '{path}' does not resolve to object '{name}'.")
        return item, path
    matches = [
        entry for entry in _iter_objects(sysman, ("POUs", "DUTs", "GVLs", "Interfaces", "VISUs"))
        if entry["itemType"] != 601 and entry["name"].casefold() == str(name).casefold()
    ]
    # TwinCAT treats PLC object names case-insensitively in many COM APIs, but
    # a visualization may legitimately differ from a POU only by case (for
    # example ``MAIN`` and ``Main``).  Preserve the caller's exact spelling
    # before declaring that lookup ambiguous.
    exact_matches = [entry for entry in matches if entry["name"] == str(name)]
    if len(exact_matches) == 1:
        return exact_matches[0]["item"], exact_matches[0]["path"]
    if exact_matches:
        matches = exact_matches
    if len(matches) == 1:
        return matches[0]["item"], matches[0]["path"]
    if len(matches) > 1:
        paths = ", ".join(entry["path"] for entry in matches)
        raise ValueError(
            f"Object '{name}' is ambiguous in the current PLC tree (multiple PLC projects or matching nodes); "
            "use plc_find and pass its exact path. "
            f"Matches: {paths}"
        )
    raise FileNotFoundError(f"Object '{name}' not found in any PLC folder.")


def _find_member(item, member_path: str, member_type: str = ""):
    target = item
    for segment in filter(None, str(member_path or "").split(".")):
        found = next((child for child in _children(target)
                      if _name(child).casefold() == segment.casefold()), None)
        if found is None:
            available = [
                f"{_name(child)} ({_MEMBER_TYPE_NAMES.get(_item_type(child), 'type' + str(_item_type(child)))})"
                for child in _children(target) if _name(child)
            ]
            hint = "; available members: " + ", ".join(available[:64]) if available else ""
            raise FileNotFoundError(
                f"Member '{segment}' not found under '{_name(target)}'.{hint}"
            )
        target = found
    wanted = str(member_type or "").casefold()
    if wanted and wanted in _MEMBER_TYPE_IDS and _item_type(target) not in _MEMBER_TYPE_IDS[wanted]:
        # ``member_type`` is supplied by an LLM/UI as a disambiguation hint.
        # Once an exact member name has been resolved, rejecting a read merely
        # because an Action was labelled a Method provides no safety benefit;
        # it just wastes a second COM round-trip.  Mutations intentionally do
        # their own strict type checks where type changes safety semantics.
        pass
    return target


def _read_text(item, area: str) -> str:
    attr = "DeclarationText" if area == "declaration" else "ImplementationText"
    try:
        value = getattr(item, attr)
        text = str(value() if callable(value) else value or "")
        return plc.normalize_twincat_text(text)[0]
    except Exception:
        return ""


def _write_text(item, area: str, value: str) -> tuple[str, int]:
    if area not in {"declaration", "implementation"}:
        raise ValueError("area must be declaration or implementation")
    cleaned, replaced = plc.normalize_twincat_text(value)
    setattr(
        item,
        "DeclarationText" if area == "declaration" else "ImplementationText",
        cleaned,
    )
    return cleaned, replaced


def _slice_lines(text: str, start_line: int, max_lines: int) -> tuple[str, dict]:
    lines = str(text or "").splitlines()
    start = max(1, int(start_line or 1))
    limit = max(0, int(max_lines or 0))
    selected = lines[start - 1:] if not limit else lines[start - 1:start - 1 + limit]
    return "\n".join(selected), {
        "start_line": start,
        "returned_lines": len(selected),
        "total_lines": len(lines),
        "truncated": bool(limit and start - 1 + limit < len(lines)),
    }


def _normalize_member_tree_path(name: str, path: str, method: str) -> tuple[str, str]:
    """Split a pasted ``POU^Member`` path before LookupTreeItem."""
    normalized_path = str(path or "")
    normalized_method = str(method or "")
    if normalized_path:
        parts = normalized_path.split("^")
        matches = [index for index, part in enumerate(parts)
                   if part.casefold() == str(name).casefold()]
        if matches and matches[-1] < len(parts) - 1:
            object_index = matches[-1]
            if not normalized_method:
                normalized_method = ".".join(parts[object_index + 1:])
            normalized_path = "^".join(parts[:object_index + 1])
    return normalized_path, normalized_method


def _read_pou(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    name = str(args.get("name") or "")
    path, method = _normalize_member_tree_path(
        name, str(args.get("path") or ""), str(args.get("method") or ""))
    item, path = _find_object(sysman, name, path)
    member_type = str(args.get("member_type") or "")
    target = _find_member(item, method, member_type) if method else item
    area = str(args.get("area") or "all")
    start_line = int(args.get("start_line") or 1)
    max_lines = int(args.get("max_lines") or 0)
    result: dict[str, Any] = {
        "name": _name(item), "path": path, "itemType": _item_type(item),
        "method": method, "methods": [],
    }
    if method:
        actual_member_type = _MEMBER_TYPE_NAMES.get(_item_type(target), f"itemType {_item_type(target)}")
        result["member_type"] = actual_member_type
        requested_member_type = member_type.casefold()
        if requested_member_type and requested_member_type in _MEMBER_TYPE_IDS \
                and _item_type(target) not in _MEMBER_TYPE_IDS[requested_member_type]:
            result["member_type_resolved"] = {
                "requested": requested_member_type,
                "actual": actual_member_type,
                "message": "已按 XAE 实际成员类型读取。",
            }
    areas = ("declaration", "implementation")
    if area in areas:
        areas = (area,)
    elif area == "members":
        areas = ()
    for current_area in areas:
        value, paging = _slice_lines(_read_text(target, current_area), start_line, max_lines)
        result[current_area] = value
        result[f"{current_area}_paging"] = paging
    include_member_code = bool(args.get("include_member_code", True))
    if not method:
        from .member_catalog import catalog
        result['member_catalog'] = catalog(item)
        for child in _children(item):
            if _item_type(child) not in _MEMBER_TYPES:
                continue
            result["methods"].append(
                _member_payload(child, include_code=include_member_code)
            )
    return result


def _save_document(dte, args: dict, *, save=False) -> dict:
    from .document_save import execute
    # Use the same first System Manager project as _system_manager. Do not
    # resolve physical filenames through the active editor or a name search.
    for project in list(dte.Solution.Projects):
        try:
            sysman = project.Object
            sysman.LookupTreeItem('TIPC')
        except Exception:
            continue
        parent, path = _find_object(sysman, args['name'], args['path'])
        return execute(dte, parent, path, str(project.FullName),
                       args.get('expected_document_baseline'), save=save)
    raise ValueError('Bound XAE has no System Manager project')


def _library_evidence(dte, args):
    from .library_evidence import inspect_reference
    sm = _system_manager(dte)
    path = str(args.get('path') or '')
    if path not in {base for base, _, _ in _plc_roots(sm)}:
        raise ValueError('Exact current PLC project root required for library evidence')
    references = sm.LookupTreeItem(path + '^References')
    results = []
    # Strict enumeration: unknown must not become an empty verified library set.
    for item in list(references):
        evidence = inspect_reference(str(item.ProduceXml(False)))
        if evidence:
            results.append(evidence)
    return {'status': 'read', 'path': path, 'libraries': results, 'signature_verified': False}


def _library_signatures(dte, args):
    from .library_signatures import read_signatures
    sm = _system_manager(dte)
    path = str(args.get('path') or '')
    if path not in {base for base, _, _ in _plc_roots(sm)}:
        raise ValueError('Exact current PLC project root required for library signatures')
    return read_signatures(sm.LookupTreeItem(path + '^References'))


def _compiler_settings(dte,args):
    from .compiler_settings import read_settings
    path=str(args.get('path') or '')
    matches=[item for base,_,item in _plc_roots(_system_manager(dte)) if base==path]
    if len(matches)!=1: raise ValueError('Exact current PLC project root required for compiler settings')
    return read_settings(dte,matches[0],path)


def _read_current(dte, args: dict) -> dict:
    """Read the active PLC editor document through the same DTE connection."""
    try:
        document = dte.ActiveDocument
        if document is None:
            raise ValueError("当前没有活动文档；请先在 XAE 中选中或打开 PLC 源码页签")
        full_name = str(document.FullName or "")
        saved = bool(document.Saved)
        display_name = str(document.Name or "")
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"无法读取当前 XAE 活动文档: {exc}") from exc
    match = re.match(r"(?is)^(.*\.(?:TcPOU|TcGVL|TcDUT|TcIO))(?:@(.+))?$", full_name)
    if not match:
        raise ValueError("当前活动文档不是可读取的 TwinCAT PLC 源码对象")
    source_file, member = match.groups()
    source_path = Path(source_file)
    name = source_path.stem
    path = ""
    lookup_strategy = "name_scan_fallback"
    lookup_error = ""
    # TwinCAT's source-folder layout normally mirrors the TIPC tree.  Build a
    # direct LookupTreeItem path from the active file instead of walking every
    # PLC object by name (which is costly in projects with hundreds of POUs).
    folder_index = next((index for index, part in enumerate(source_path.parts)
                         if part.casefold() in {"pous", "duts", "gvls", "interfaces", "visus"}), None)
    if folder_index is not None:
        source_parts = list(source_path.parts[folder_index:])
        source_parts[-1] = source_path.stem
        suffix = "^".join(source_parts)
        for base, _, _ in _plc_roots(_system_manager(dte)):
            candidate = f"{base}^{suffix}"
            try:
                item = _system_manager(dte).LookupTreeItem(candidate)
                if _name(item).casefold() == name.casefold():
                    path = candidate
                    lookup_strategy = "active_document_path"
                    break
            except Exception:
                lookup_error = candidate
                continue
    request = {
        "name": name, "path": path, "method": member or "", "area": str(args.get("area") or "all"),
        "member_type": str(args.get("member_type") or ""),
        "include_member_code": False, "start_line": 1, "max_lines": 0,
    }
    result = _read_pou(dte, request)
    result["active_document"] = {
        "name": display_name, "full_name": full_name, "source_file": source_file,
        "saved": saved, "member": member or "",
    }
    result["current_lookup_strategy"] = lookup_strategy
    if lookup_error and lookup_strategy != "active_document_path":
        result["current_lookup_candidate"] = lookup_error
    return result


def _read_batch(dte, args: dict) -> dict:
    """Read several live XAE code slices through one DTE/helper connection."""
    requests = list(args.get("requests") or [])
    if not requests or len(requests) > 16:
        raise ValueError("requests must contain between 1 and 16 items")
    max_total_chars = max(1000, min(int(args.get("max_total_chars") or 50000), 200000))
    started = time.perf_counter()
    results = []
    used_chars = 0
    for index, raw in enumerate(requests):
        request = dict(raw or {})
        if not str(request.get("name") or "").strip():
            raise ValueError(f"requests[{index}].name is required")
        request.setdefault("area", "all")
        request.setdefault("start_line", 1)
        request.setdefault("max_lines", 120)
        request.setdefault("include_member_code", False)
        result = _read_pou(dte, request)
        known = dict(request.get("known_hashes") or {})
        hashes = {}
        unchanged = []
        for area in ("declaration", "implementation"):
            if area not in result:
                continue
            text = str(result.get(area) or "")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            hashes[area] = digest
            if str(known.get(area) or "") == digest:
                result[area] = ""
                unchanged.append(area)
                continue
            remaining = max_total_chars - used_chars
            if remaining <= 0:
                result[area] = ""
                result[f"{area}_omitted"] = "batch character budget exhausted"
            elif len(text) > remaining:
                result[area] = text[:remaining]
                result[f"{area}_omitted_chars"] = len(text) - remaining
                used_chars += remaining
            else:
                used_chars += len(text)
        result["hashes"] = hashes
        result["unchanged_areas"] = unchanged
        result["request_index"] = index
        result["request_id"] = str(request.get("id") or index)
        results.append(result)
    return {
        "status": "read", "count": len(results), "results": results,
        "total_chars": used_chars, "max_total_chars": max_total_chars,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "live_xae": True,
    }


def _new_folder(dte, args: dict) -> dict:
    """Create an XAE PLC folder below an exact tree node.

    TwinCAT permits folders below the PLC project tree and, depending on the
    XAE version, below a POU as an organisational container.  Keep the parent
    explicit so a localized/nested project cannot receive an object by guess.
    """
    sysman = _system_manager(dte)
    name = str(args.get("name") or "").strip()
    parent_path = str(args.get("parent_path") or "").strip()
    if not name or "^" in name:
        raise ValueError("folder name must be a non-empty single tree segment")
    bases = [base for base, _, _ in _plc_roots(sysman)]
    if not parent_path:
        if not bases:
            raise RuntimeError("No PLC project found in the current solution.")
        parent_path = f"{bases[0]}^POUs"
    if not any(parent_path.casefold() == base.casefold()
               or parent_path.casefold().startswith((base + "^").casefold())
               for base in bases):
        raise ValueError(f"Parent tree path is outside the current PLC project: {parent_path}")
    try:
        parent = sysman.LookupTreeItem(parent_path)
    except Exception as exc:
        raise FileNotFoundError(f"Parent tree path was not found: {parent_path}") from exc
    if any(_name(child).casefold() == name.casefold() for child in _children(parent)):
        raise ValueError(f"'{name}' already exists under '{parent_path}'")
    parent.CreateChild(name, 601, "", None)
    time.sleep(0.25)
    child_path = f"{parent_path}^{name}"
    return {"name": name, "parent_path": parent_path, "path": child_path,
            "itemType": 601, "status": "created"}


def _prepare_dut_declaration(item, code: str) -> str:
    """Normalize DUT source and block impossible in-place subtype changes."""
    if _item_type(item) in {605, 606, 607, 623}:
        dut_kind = {605: "enum", 606: "struct", 607: "union", 623: "alias"}[
            _item_type(item)
        ]
        inferred_kind = dut_kind
        if re.search(r"(?is)\bSTRUCT\b", code):
            inferred_kind = "struct"
        elif re.search(r"(?is)\bUNION\b", code):
            inferred_kind = "union"
        elif re.search(r"(?is)(?:^|:)\s*\(", code):
            inferred_kind = "enum"
        if inferred_kind != dut_kind:
            raise ValueError(
                f"DUT '{_name(item)}' is itemType {_item_type(item)} ({dut_kind}) and cannot "
                f"be converted to {inferred_kind} with plc_write; delete and recreate it with "
                f"plc_create(type='{inferred_kind}')."
            )
        return plc.normalize_dut_declaration(dut_kind, _name(item), code)
    return code


def _write_pou(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    name = str(args.get("name") or "")
    path, method = _normalize_member_tree_path(
        name, str(args.get("path") or ""), str(args.get("method") or ""))
    item, path = _find_object(sysman, name, path)
    target = _find_member(item, method) if method else item
    area = str(args.get("area") or "implementation")
    if 'paired_implementation' in args:
        if method or area != 'declaration' or _item_type(target) not in {602, 603, 604}:
            raise ValueError('Paired write supports top-level ST POU declaration/implementation only')
        from .paired_write import write_pair
        return write_pair(target, args['code'], args['paired_implementation'], args.get('paired_baseline'))
    # Interface members expose their signature through CreateChild's vInfo.
    # TcXaeShell 15 can terminate when a child interface item's text property
    # is assigned through late-bound COM (610 method, 612 property and
    # 654/655 accessors), therefore expose all of them as read-only rather
    # than risking the IDE/process.
    if _item_type(target) in {610, 612, 654, 655}:
        raise ValueError(
            "Interface member text is read-only on this XAE COM bridge; "
            "specify the member return_type when creating it."
        )
    code = str(args.get("code") or "")
    if not method and area == "declaration":
        if _item_type(item) == 618:
            code = plc.validate_interface_declaration(_name(item), code)
        code = _prepare_dut_declaration(item, code)
    cleaned, sanitized_chars = _write_text(target, area, code)
    return {"name": _name(item), "path": path, "method": method,
            "area": area, "status": "written", "chars": len(cleaned),
            "sanitized_chars": sanitized_chars}


def _patch_pou(dte, args: dict) -> dict:
    old = str(args.get("old_text") or "")
    if not old:
        raise ValueError("old_text must not be empty")
    sysman = _system_manager(dte)
    name = str(args.get("name") or "")
    path, method = _normalize_member_tree_path(
        name, str(args.get("path") or ""), str(args.get("method") or ""))
    item, path = _find_object(sysman, name, path)
    target = _find_member(item, method) if method else item
    area = str(args.get("area") or "implementation")
    current = _read_text(target, area)
    count = current.count(old)
    if count == 0:
        raise ValueError("old_text was not found")
    if count > 1:
        raise ValueError("old_text occurs more than once; provide a larger unique block")
    updated = current.replace(old, str(args.get("new_text") or ""), 1)
    if not method and area == "declaration":
        if _item_type(item) == 618:
            updated = plc.validate_interface_declaration(_name(item), updated)
        updated = _prepare_dut_declaration(item, updated)
    cleaned, sanitized_chars = _write_text(target, area, updated)
    return {"name": _name(item), "path": path, "method": method, "area": area,
            "status": "patched", "chars_before": len(current),
            "chars_after": len(cleaned), "sanitized_chars": sanitized_chars}


def _find_pou(dte, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    folder_filter = str(args.get("folder") or "")
    limit = max(1, min(int(args.get("limit") or 30), 200))
    include_members = bool(args.get("include_members", True))
    sysman = _system_manager(dte)
    folders = (folder_filter,) if folder_filter else _FOLDERS
    found = []
    folded = query.casefold()
    for entry in _iter_objects(sysman, folders):
        if entry["itemType"] == 601:
            continue
        name_folded = entry["name"].casefold()
        if folded in name_folded:
            score = 0 if name_folded == folded else 1 if name_folded.startswith(folded) else 2
            found.append({"score": score, "name": entry["name"], "folder": entry["folder"],
                          "kind": _KIND.get(entry["itemType"], "object"),
                          "itemType": entry["itemType"], "path": entry["path"],
                          "parent": entry["parent"]})
        if include_members:
            for child in _children(entry["item"]):
                if _item_type(child) not in _MEMBER_TYPES:
                    continue
                member_name = _name(child)
                qualified = f"{entry['name']}.{member_name}"
                if folded in qualified.casefold():
                    found.append({"score": 3, "name": entry["name"], "folder": entry["folder"],
                                  "kind": "member", "itemType": entry["itemType"],
                                  "path": entry["path"], "parent": entry["parent"],
                                  "member": member_name, "member_itemType": _item_type(child),
                                  "member_path": f"{entry['path']}^{member_name}"})
    found.sort(key=lambda item: (item["score"], item["name"].casefold(),
                                 str(item.get("member") or "").casefold()))
    exact_objects = [item for item in found
                     if item["score"] == 0 and not item.get("member")]
    selected = exact_objects if len(exact_objects) == 1 else found
    return {"query": query, "exact": len(exact_objects) == 1,
            "total": len(found), "matches": selected[:limit]}


def _structure(dte, args: dict) -> dict:
    limit = max(0, int(args.get("limit_per_folder") or 0))
    include_members = bool(args.get("include_members", True))
    sysman = _system_manager(dte)
    projects = [name for _, name, _ in _plc_roots(sysman)]
    folders: dict[str, list] = {}
    counts: dict[str, int] = {name: 0 for name in _FOLDERS}
    truncated = False
    for entry in _iter_objects(sysman):
        folder_name = entry["folder"]
        counts[folder_name] += 1
        entries = folders.setdefault(folder_name, [])
        if limit and len(entries) >= limit:
            truncated = True
            continue
        methods = []
        member_details = []
        if include_members:
            members = [c for c in _children(entry["item"])
                       if _item_type(c) in _MEMBER_TYPES and _name(c)]
            # Keep the long-standing string list for callers that only render
            # top-level members, and expose the richer tree separately.
            methods = [_name(c) for c in members]
            member_details = [_member_payload(c, include_code=False) for c in members]
        entries.append({k: entry[k] for k in
                        ("name", "itemType", "path", "parent", "depth")} |
                       {"kind": _KIND.get(entry["itemType"], "object"),
                        "methods": methods, "memberDetails": member_details})
    folders = {name: folders[name] for name in _FOLDERS if folders.get(name)}
    return {"project": projects[0] if len(projects) == 1 else "",
            "projects": projects, "counts": counts, "folders": folders,
            "truncated": truncated, "limit_per_folder": limit,
            "members_included": include_members}


def _solution_tree(dte, args: dict) -> dict:
    """Return the live XAE System Manager tree for the context picker.

    This reads Automation Interface nodes instead of walking the solution
    directory on disk. Returned paths are native tree paths such as
    ``TIPC^PLC1^PLC1项目^POUs``.
    """
    sysman = _system_manager(dte)
    solution_name = Path(str(dte.Solution.FullName or "")).stem or "当前解决方案"
    project_name = solution_name
    try:
        project_name = str(dte.Solution.Projects.Item(1).Name or solution_name)
    except Exception:
        pass
    nodes: list[dict[str, Any]] = []

    def add(path: str, name: str, parent: str, item_type: int = 0, synthetic: bool = False):
        if not name or any(item["path"] == path for item in nodes):
            return
        nodes.append({
            "path": path, "name": name, "parent": parent,
            "itemType": item_type, "synthetic": synthetic,
        })

    add("PROJECT", project_name, ".", synthetic=True)
    add("SYSTEM", "系统", "PROJECT", synthetic=True)
    add("SYSTEM^授权", "授权", "SYSTEM", synthetic=True)
    add("SYSTEM^实时", "实时", "SYSTEM", synthetic=True)

    roots = (
        ("TIRT", "任务", "SYSTEM"),
        ("TIRR", "路由表", "SYSTEM"),
        ("TINC", "运动控制", "PROJECT"),
        ("TIPC", "PLC", "PROJECT"),
        ("TISC", "SAFETY", "PROJECT"),
        ("TIXC", "C++", "PROJECT"),
        ("TIVS", "VISION", "PROJECT"),
        ("TIAN", "ANALYTICS", "PROJECT"),
        ("TIID", "I/O", "PROJECT"),
    )

    def walk(item, path: str, depth: int = 0):
        if depth >= 12 or len(nodes) >= 2500:
            return
        for child in _children(item):
            child_name = _name(child)
            if not child_name:
                continue
            child_path = f"{path}^{child_name}"
            add(child_path, child_name, path, _item_type(child))
            walk(child, child_path, depth + 1)

    for code, label, parent in roots:
        try:
            item = sysman.LookupTreeItem(code)
        except Exception:
            continue
        add(code, label, parent, _item_type(item))
        walk(item, code)
    return {"root": solution_name, "nodes": nodes}


def _search_code(dte, args: dict) -> dict:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise ValueError("pattern is required")
    regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    max_results = max(1, min(int(args.get("max_results") or 50), 500))
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern, flags) if regex else None
    needle = pattern if case_sensitive else pattern.casefold()

    def matches(line: str) -> bool:
        return bool(matcher.search(line)) if matcher else needle in (
            line if case_sensitive else line.casefold())

    sysman = _system_manager(dte)
    pou = str(args.get("pou") or "")
    path = str(args.get("path") or "")
    entries = []
    if pou:
        item, resolved = _find_object(sysman, pou, path)
        entries = [{"item": item, "name": _name(item), "path": resolved}]
    else:
        entries = [entry for entry in _iter_objects(
            sysman, ("POUs", "DUTs", "GVLs", "Interfaces")) if entry["itemType"] != 601]
    results = []
    for entry in entries:
        targets = [(entry["item"], entry["name"], entry["path"])]
        targets.extend((child, f"{entry['name']}.{_name(child)}", entry["path"])
                       for child in _children(entry["item"])
                       if _item_type(child) in _MEMBER_TYPES)
        for target, target_name, target_path in targets:
            for area in ("declaration", "implementation"):
                for line_no, line in enumerate(_read_text(target, area).splitlines(), 1):
                    if matches(line):
                        results.append({"pou": target_name, "path": target_path,
                                        "area": area, "line": line_no,
                                        "text": line.strip()[:500]})
                        if len(results) >= max_results:
                            return {"pattern": pattern, "pou": pou, "path": path,
                                    "count": len(results), "truncated": True,
                                    "max_results": max_results, "matches": results}
    return {"pattern": pattern, "pou": pou, "path": path, "count": len(results),
            "truncated": False, "max_results": max_results, "matches": results}


def _new_member(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    pou_name = str(args.get("pou") or "")
    parent, _ = _find_object(sysman, pou_name, str(args.get("path") or ""))
    member_type = str(args.get("type") or "method")
    is_interface = _item_type(parent) == 618
    # TcXaeShell 15 has been observed to terminate while creating interface
    # property accessors (654/655) through late-bound Automation Interface.
    # Do not expose an operation that can bring down the IDE.  Interface
    # methods/properties themselves remain creatable and readable.
    if is_interface and member_type in {"property", "propget", "propset"}:
        raise ValueError(
            "Interface properties are disabled on this XAE COM bridge because "
            "TwinCAT can terminate while creating their accessors; use an interface "
            "method or create the complete property manually in XAE."
        )
    pou_map = {"action": 608, "method": 609, "property": 611,
               "propget": 613, "propset": 614, "transition": 616}
    interface_map = {"method": 610, "property": 612, "propget": 654, "propset": 655}
    subtype = (interface_map if is_interface else pou_map).get(member_type)
    if subtype is None:
        raise ValueError(f"member type '{member_type}' is not supported")
    name = str(args.get("name") or "")
    return_type = str(args.get("return_type") or "BOOL")
    language = str(args.get("language") or "ST")
    if member_type in {"propget", "propset"}:
        property_item = _find_member(parent, name)
        accessor_name = "Get" if member_type == "propget" else "Set"
        property_item.CreateChild(accessor_name, subtype, "",
                                  None if is_interface else language)
        time.sleep(0.4)
        created = _find_member(property_item, accessor_name)
    else:
        if is_interface:
            # Interface methods/properties accept only their return type.
            vinfo = return_type
        elif member_type in {"method", "property"}:
            # POU methods/properties require [IEC language, return type].
            vinfo = [language, return_type]
        else:
            # Actions and transitions only take the IEC language.
            vinfo = language
        parent.CreateChild(name, subtype, "", vinfo)
        time.sleep(0.4)
        created = _find_member(parent, name)
    sanitized_chars = 0
    if args.get("declaration"):
        _, count = _write_text(created, "declaration", str(args["declaration"]))
        sanitized_chars += count
    if args.get("implementation"):
        _, count = _write_text(created, "implementation", str(args["implementation"]))
        sanitized_chars += count
    return {"pou": pou_name, "member": name, "type": member_type,
            "parentKind": "interface" if is_interface else "pou", "status": "created",
            "sanitized_chars": sanitized_chars}


def _delete_member(dte, args: dict) -> dict:
    """Delete a POU/interface child or one property accessor by exact name."""
    sysman = _system_manager(dte)
    pou_name = str(args.get("pou") or "")
    member_name = str(args.get("name") or "")
    member_type = str(args.get("type") or "method").lower()
    parent, resolved_path = _find_object(
        sysman, pou_name, str(args.get("path") or "")
    )
    valid = {"method", "property", "action", "transition", "propget", "propset"}
    if member_type not in valid:
        raise ValueError(f"member type '{member_type}' is not supported")

    dry_run = bool(args.get("dry_run", False))
    force = bool(args.get("force", False))
    references = _symbol_references(dte, member_name, f"{pou_name}.{member_name}")
    if dry_run:
        return {"pou": pou_name, "member": member_name, "type": member_type,
                "path": resolved_path, "status": "preview",
                "would_block": bool(references and not force),
                "reference_count": len(references), "references": references}
    if references and not force:
        return {"pou": pou_name, "member": member_name, "type": member_type,
                "path": resolved_path, "status": "blocked", "error":
                f"Member has {len(references)} reference(s); inspect references or pass force=true.",
                "reference_count": len(references), "references": references}
    checked_tree = None
    if args.get('expected_member_baseline') is not None:
        from .member_baseline import capture, conflict, expected_tree
        try:
            token, tree = capture(dte, parent, resolved_path)
            if token != args['expected_member_baseline']:
                return conflict('PLC member baseline changed; deletion was not executed')
            target_name = member_name + ('.Get' if member_type == 'propget' else '.Set' if member_type == 'propset' else '')
            checked_tree = expected_tree(tree, target_name)
        except Exception as exc:
            return conflict(exc)
    if member_type in {"propget", "propset"}:
        property_item = _find_member(parent, member_name)
        accessor_name = "Get" if member_type == "propget" else "Set"
        accessor = _find_member(property_item, accessor_name)
        try:
            property_item.DeleteChild(_name(accessor))
        except Exception as exc:
            if checked_tree is not None:
                return conflict(exc, written='unknown')
            raise
        deleted_name = f"{member_name}.{accessor_name}"
    else:
        member = _find_member(parent, member_name)
        expected = {
            "action": {608}, "method": {609, 610}, "property": {611, 612},
            "transition": {616},
        }[member_type]
        if _item_type(member) not in expected:
            raise ValueError(
                f"Member '{member_name}' exists under '{pou_name}' but is not a {member_type}"
            )
        try:
            container = _find_member(parent, member_name.rsplit('.', 1)[0]) if '.' in member_name else parent
            container.DeleteChild(_name(member))
        except Exception as exc:
            if checked_tree is not None:
                return conflict(exc, written='unknown')
            raise
        deleted_name = member_name
    if checked_tree is not None:
        try:
            # Delete invalidates COM wrappers/enumerators. Resolve the exact
            # parent again; never verify through the pre-mutation wrapper.
            parent, fresh_path = _find_object(sysman, pou_name, resolved_path)
            if fresh_path != resolved_path:
                return conflict('Member deletion parent identity changed', written=True)
            token, actual_tree = capture(dte, parent, resolved_path)
            if actual_tree != checked_tree:
                return conflict('Member deletion readback differs from the expected tree', written=True)
        except Exception as exc:
            return conflict(exc, written=True)
        return {'status': 'deleted', 'written': True, 'verified': True,
                'pou': pou_name, 'member': deleted_name, 'path': resolved_path,
                'member_baseline': token, 'reference_count': len(references), 'references': references}
    return {"pou": pou_name, "member": deleted_name, "type": member_type,
            "path": resolved_path, "status": "deleted",
            "reference_count": len(references), "references": references}


def _symbol_references(dte, symbol: str, exclude_pou: str = "") -> list[dict]:
    result = _search_code(dte, {
        "pattern": rf"\b{re.escape(str(symbol))}\b", "regex": True,
        "case_sensitive": False, "max_results": 100,
    })
    prefix = str(exclude_pou or "").casefold()
    return [item for item in result.get("matches", [])
            if not (str(item.get("pou") or "").casefold() == prefix or
                    str(item.get("pou") or "").casefold().startswith(prefix + "."))][:50]


def _delete_object(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    name = str(args.get("name") or "")
    item, path = _find_object(sysman, name, str(args.get("path") or ""))
    references = _symbol_references(dte, name, name)
    if bool(args.get("dry_run", False)):
        return {"name": name, "path": path, "status": "preview",
                "would_block": bool(references and not bool(args.get("force", False))),
                "reference_count": len(references), "references": references}
    if references and not bool(args.get("force", False)):
        return {"name": name, "path": path, "status": "blocked", "error":
                f"Object has {len(references)} reference(s); inspect references or pass force=true.",
                "reference_count": len(references), "references": references}
    parent_path = path.rsplit("^", 1)[0]
    parent = sysman.LookupTreeItem(parent_path)
    parent.DeleteChild(_name(item))
    return {"name": name, "path": path, "folder": _CATEGORY_BY_TYPE.get(_item_type(item), ""),
            "status": "deleted", "reference_count": len(references),
            "references": references}


def _rename_object(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    item, path = _find_object(sysman, old, str(args.get("path") or ""))
    parent = sysman.LookupTreeItem(path.rsplit("^", 1)[0])
    if any(_name(child).casefold() == new.casefold() for child in _children(parent)):
        raise ValueError(f"'{new}' already exists beside '{old}'")
    item.Name = new
    return {"old": old, "new": new, "path": path, "status": "renamed"}


def _rename_member(dte, args: dict) -> dict:
    sysman = _system_manager(dte)
    pou = str(args.get("pou") or "")
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    parent, path = _find_object(sysman, pou, str(args.get("path") or ""))
    member = _find_member(parent, old)
    if any(_name(child).casefold() == new.casefold() for child in _children(parent)):
        raise ValueError(f"Member '{new}' already exists in '{pou}'")
    checked_tree = None
    if args.get('expected_member_baseline') is not None:
        from .member_baseline import capture, conflict, expected_tree, renamed_declaration
        try:
            token, tree = capture(dte, parent, path)
            if token != args['expected_member_baseline']:
                return conflict('PLC member baseline changed; rename was not executed')
            checked_tree = expected_tree(tree, old, new)
            old_declaration = str(member.DeclarationText) if _item_type(member) in {609, 610, 611, 612} else None
        except Exception as exc:
            return conflict(exc)
    try:
        member.Name = new
    except Exception as exc:
        if checked_tree is not None:
            return conflict(exc, written='unknown')
        raise
    if checked_tree is not None:
        try:
            parent, fresh_path = _find_object(sysman, pou, path)
            if fresh_path != path:
                return conflict('Member rename parent identity changed', written=True)
            token, actual_tree = capture(dte, parent, path)
            if actual_tree != checked_tree and old_declaration is not None:
                checked_tree = expected_tree(tree, old, new, renamed_declaration(old_declaration, old, new))
            if actual_tree != checked_tree:
                return conflict('Member rename readback differs from the expected tree', written=True)
        except Exception as exc:
            return conflict(exc, written=True)
        return {'status': 'renamed', 'written': True, 'verified': True,
                'pou': pou, 'old': old, 'new': new, 'path': path, 'member_baseline': token}
    return {"pou": pou, "old": old, "new": new, "path": path, "status": "renamed"}


def _read_member_baseline(dte, args):
    from .member_baseline import capture, conflict
    if not args.get('path'):
        return conflict('An exact parent PLC path is required')
    try:
        parent, path = _find_object(_system_manager(dte), args['name'], args['path'])
        token, tree = capture(dte, parent, path)
        return {'status': 'read', 'path': path, 'name': args['name'],
                'member_baseline': token, 'member_tree': tree, 'live_xae': True}
    except Exception as exc:
        return conflict(exc)


def _area_payload(text: str, include_code: bool) -> dict:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = {"hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
              "chars": len(normalized), "lines": len(normalized.splitlines())}
    if include_code:
        result["text"] = normalized
    return result


def _code_inventory(dte, args: dict) -> dict:
    include_code = bool(args.get("include_code", False))
    selected = {str(path).casefold() for path in (args.get("paths") or [])}
    sysman = _system_manager(dte)
    bases = [base for base, _, _ in _plc_roots(sysman)]
    objects = []
    for entry in _iter_objects(sysman, ("POUs", "DUTs", "GVLs", "Interfaces")):
        if entry["itemType"] == 601 or (selected and entry["path"].casefold() not in selected):
            continue
        item = entry["item"]
        members = []
        for child in _children(item):
            if _item_type(child) not in _MEMBER_TYPES:
                continue
            members.append({"name": _name(child), "path": f"{entry['path']}^{_name(child)}",
                            "itemType": _item_type(child),
                            "declaration": _area_payload(_read_text(child, "declaration"), include_code),
                            "implementation": _area_payload(_read_text(child, "implementation"), include_code)})
        objects.append({"name": entry["name"], "kind": _KIND.get(entry["itemType"], "object"),
                        "folder": entry["folder"], "path": entry["path"],
                        "itemType": entry["itemType"],
                        "declaration": _area_payload(_read_text(item, "declaration"), include_code),
                        "implementation": _area_payload(_read_text(item, "implementation"), include_code),
                        "members": members})
    return {"solution": str(dte.Solution.FullName or ""), "project_path": bases[0] if len(bases) == 1 else "",
            "project_paths": bases,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "include_code": include_code, "object_count": len(objects), "objects": objects}


def _code_inventory_command(dte, args: dict) -> dict:
    result = _code_inventory(dte, args)
    output_file = str(args.get("output_file") or "")
    if not output_file:
        return result
    destination = Path(output_file).expanduser().resolve()
    destination.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return {"written_to": str(destination), "object_count": result["object_count"]}


def _error_item_payload(item) -> dict:
    level = int(getattr(item, "ErrorLevel", 0) or 0)
    # EnvDTE80.vsBuildErrorLevel: Low=1, Medium=2, High=4.
    severity = {1: "message", 2: "warning", 4: "error"}.get(level, "message")
    return {
        "severity": severity,
        "code": str(getattr(item, "ErrorCode", "") or ""),
        "description": str(getattr(item, "Description", "") or ""),
        "project": str(getattr(item, "Project", "") or ""),
        "file": str(getattr(item, "FileName", "") or ""),
        "line": int(getattr(item, "Line", 0) or 0),
    }


def _read_error_items_com(dte) -> list[dict]:
    """Try the non-invasive DTE ErrorItems API first."""
    try:
        items = retry_com_busy(lambda: dte.ToolWindows.ErrorList.ErrorItems)
        count = int(retry_com_busy(lambda: items.Count) or 0)
        return [
            _error_item_payload(retry_com_busy(lambda index=index: items.Item(index)))
            for index in range(1, count + 1)
        ]
    except Exception:
        return []


def _is_build_error(item: dict, failed_projects: int) -> bool:
    """Classify compiler diagnostics that TcXaeShell reports as Medium.

    TwinCAT PLC compiler diagnostics are sometimes exposed as
    ``ErrorLevel=Medium`` rather than ``High``.  Once SolutionBuild reports a
    failed project, a project/file diagnostic or a C/T compiler code is an
    error, even if the Automation Interface assigned it warning severity.
    Ordinary warnings remain warnings when they have no compiler signature.
    """
    if item.get("severity") == "error":
        return True
    if failed_projects <= 0:
        return False
    project = str(item.get("project") or "").strip()
    file_name = str(item.get("file") or "").strip()
    code = str(item.get("code") or "").strip()
    description = str(item.get("description") or "").strip()
    return bool(project and (file_name or re.match(r"^(?:C|T)\d{4}\b", code, re.I)
                             or re.match(r"^(?:C|T)\d{4}\s*:", description, re.I)))


def _parse_error_list_tsv(text: str) -> list[dict]:
    result: list[dict] = []
    for raw_line in str(text or "").splitlines():
        if not raw_line.strip():
            continue
        columns = raw_line.split("\t")
        if len(columns) < 3:
            continue
        raw_severity = columns[0].strip()
        if raw_severity in {"严重性", "Severity"}:
            continue
        if re.search(r"错误|Error", raw_severity, re.IGNORECASE):
            severity = "error"
        elif re.search(r"警告|Warning", raw_severity, re.IGNORECASE):
            severity = "warning"
        else:
            severity = "message"
        description = columns[2].strip()
        code = columns[1].strip()
        if not code:
            match = re.match(r"\s*(C\d+)\s*:", description, re.IGNORECASE)
            if match:
                code = match.group(1).upper()
        try:
            line_number = int(columns[5].strip()) if len(columns) >= 6 else 0
        except ValueError:
            line_number = 0
        result.append({
            "severity": severity,
            "code": code,
            "description": description,
            "project": columns[3].strip() if len(columns) >= 4 else "",
            "file": columns[4].strip() if len(columns) >= 5 else "",
            "line": line_number,
        })
    return result


def _clipboard_get_text() -> str | None:
    import win32clipboard

    for _ in range(12):
        try:
            win32clipboard.OpenClipboard(None)
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
                return None
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return None


def _clipboard_set_text(value: str | None) -> None:
    import win32clipboard

    for _ in range(12):
        try:
            win32clipboard.OpenClipboard(None)
            try:
                win32clipboard.EmptyClipboard()
                if value is not None:
                    win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)


def _send_ctrl_key(key: str) -> None:
    import ctypes

    vk_control = 0x11
    key_up = 0x0002
    vk = ord(key.upper())
    user32 = ctypes.windll.user32
    user32.keybd_event(vk_control, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.06)
    user32.keybd_event(vk, 0, key_up, 0)
    user32.keybd_event(vk_control, 0, key_up, 0)


def _read_error_items_ui(dte) -> list[dict]:
    """Read TcXaeShell 15 Error List through Win32 focus/copy fallback."""
    import ctypes

    user32 = ctypes.windll.user32
    previous_window = int(user32.GetForegroundWindow() or 0)
    previous_clipboard = _clipboard_get_text()
    xae_window = 0
    copied = ""
    try:
        xae_window = int(retry_com_busy(lambda: dte.MainWindow.HWnd) or 0)
        try:
            retry_com_busy(lambda: dte.MainWindow.Activate())
        except Exception:
            pass
        if xae_window:
            if user32.IsIconic(xae_window):
                user32.ShowWindow(xae_window, 9)
            user32.SetForegroundWindow(xae_window)
            time.sleep(0.35)
        retry_com_busy(lambda: dte.ExecuteCommand("View.ErrorList"))
        time.sleep(0.6)
        _clipboard_set_text("")
        _send_ctrl_key("a")
        time.sleep(0.15)
        _send_ctrl_key("c")
        time.sleep(0.45)
        copied = _clipboard_get_text() or ""
    except Exception:
        copied = ""
    finally:
        _clipboard_set_text(previous_clipboard)
        if previous_window and previous_window != xae_window:
            try:
                user32.SetForegroundWindow(previous_window)
            except Exception:
                pass
    return _parse_error_list_tsv(copied)


def _read_build_errors(dte, failed_projects: int = 0) -> tuple[list[dict], str, int]:
    """Read diagnostics without ever opening or copying the Error List.

    TcXaeShell's documented ErrorItems automation API is safe to query.  Its
    old Win32 fallback activated the IDE and sent Ctrl+A/Ctrl+C, which made a
    background build steal focus.  An empty result is preferable to changing
    the user's foreground window; the embedded VSIX can obtain diagnostics on
    the XAE UI thread when available.
    """
    # Build(True) waits for the compiler, but the Error List collection is
    # populated by a separate XAE/UI update.  A single immediate read is a
    # race and was the main cause of failed builds being reported without
    # diagnostics.  Keep the retry window short and entirely COM-based.
    attempts = 0
    items: list[dict] = []
    for attempts in range(1, 6):
        items = _read_error_items_com(dte)
        if items and (failed_projects <= 0 or
                      any(_is_build_error(item, failed_projects) for item in items)):
            break
        if attempts < 5:
            time.sleep(0.12 * attempts)
    return items, "dte-error-items" if items else "unavailable", attempts


def _diagnostics(dte) -> dict:
    """Enumerate the bound IDE ErrorItems without building or changing focus.

    Unlike the legacy helper, distinguish an empty collection from COM failure.
    An existing list is never evidence that a new build succeeded.
    """
    try:
        solution = str(dte.Solution.FullName or '')
        build = dte.Solution.SolutionBuild
        state = int(build.BuildState)
        failed = int(build.LastBuildInfo)
        collection = dte.ToolWindows.ErrorList.ErrorItems
        count = int(collection.Count)
        items = [_error_item_payload(collection.Item(i)) for i in range(1, count + 1)]
        stable = (int(collection.Count) == count and int(build.BuildState) == state
                  and int(build.LastBuildInfo) == failed and str(dte.Solution.FullName or '') == solution)
        errors = [{**i, 'severity': 'error'} for i in items if _is_build_error(i, failed)]
        return {'ok': True, 'solution': solution, 'buildPerformed': False,
                'compiler_verified': False, 'failedProjects': failed,
                'errorCount': len(errors), 'errors': errors,
                'warnings': [i for i in items if not _is_build_error(i, failed)],
                'diagnosticsAvailable': True,
                'diagnosticsPending': not stable or state != 3 or (failed > 0 and not errors),
                'errorSource': 'dte-error-items-readonly', 'verification_scope': 'existing-error-list'}
    except Exception as exc:
        return {'status': 'incomplete', 'buildPerformed': False,
                'diagnosticsAvailable': False, 'diagnostics_complete': False,
                'error': 'Read-only COM diagnostics unavailable: ' + str(exc)}


def _build_command_state(dte, name: str) -> dict:
    """Read the shell's actual Build/Rebuild command availability.

    ``IsAvailable`` is a read-only EnvDTE command query.  Do not infer
    Rebuild from PLC online state or from a hard-coded TwinCAT version.
    """
    try:
        command = retry_com_busy(lambda: dte.Commands.Item(name))
    except Exception as exc:
        return {"name": name, "available": False,
                "source": "dte-command-lookup", "error": str(exc)[:200]}
    try:
        available = getattr(command, "IsAvailable")
        return {"name": name, "available": bool(available),
                "source": "dte-command-isavailable"}
    except Exception as exc:
        return {"name": name, "available": None,
                "source": "dte-command-lookup", "error": str(exc)[:200]}


def _build_state(dte, args: dict) -> dict:
    """Return live, non-mutating SolutionBuild and command state."""
    solution_build = retry_com_busy(lambda: dte.Solution.SolutionBuild)
    raw_state = None
    last_build_info = None
    active = ""
    try:
        raw_state = int(retry_com_busy(lambda: solution_build.BuildState))
    except Exception:
        pass
    try:
        last_build_info = int(retry_com_busy(lambda: solution_build.LastBuildInfo))
    except Exception:
        pass
    try:
        active = str(retry_com_busy(
            lambda: solution_build.ActiveConfiguration.Name) or "")
    except Exception:
        pass
    # TcXaeShell's current EnvDTE contract reports 1=NotStarted,
    # 2=InProgress, 3=Done.  Unknown values remain unknown rather than being
    # treated as idle, which would allow a concurrent build to be triggered.
    busy = (raw_state == 2) if raw_state in {1, 2, 3} else None
    return {
        "status": "read",
        "solution": str(dte.Solution.FullName or ""),
        "build_state": raw_state,
        "last_build_info": last_build_info,
        "active_configuration": active,
        "busy": busy,
        "commands": {
            "build": _build_command_state(dte, "Build.BuildSolution"),
            "rebuild": _build_command_state(dte, "Build.RebuildSolution"),
        },
        "source": "dte-solution-build-and-commands",
    }


def _build(dte, args: dict) -> dict:
    solution_build = retry_com_busy(lambda: dte.Solution.SolutionBuild)
    action = str(args.get("action") or "build").strip().lower()
    if action == "rebuild":
        retry_com_busy(lambda: dte.ExecuteCommand("Build.RebuildSolution"),
                       attempts=12, max_delay=1.5)
        # RebuildSolution is a shell command and therefore asynchronous on
        # some TcXaeShell versions.  Wait only for the documented Done value;
        # an unknown state is left to the caller as incomplete evidence.
        observed_running = False
        completed = False
        for attempt in range(240):
            try:
                state = int(retry_com_busy(lambda: solution_build.BuildState))
                observed_running = observed_running or state == 2
                if observed_running and state == 3:
                    completed = True
                    break
            except Exception:
                pass
            if not observed_running and attempt >= 9:
                break
            time.sleep(0.5)
        if not completed:
            return {"status": "uncertain", "uncertain": True,
                    "buildPerformed": True, "not_executed": False,
                    "compiler_verified": False, "build_action": action,
                    "error": "Rebuild 已派发，但未观测到本次 Running→Done；历史 Done 不能证明本次完成。",
                    "next_action": "只读检查构建状态和诊断；不要自动重发 Rebuild。"}
    else:
        retry_com_busy(lambda: solution_build.Build(True), attempts=12, max_delay=1.5)
    failed = int(retry_com_busy(lambda: solution_build.LastBuildInfo) or 0)
    always_read = bool(args.get("always_read_errors", False))
    if not failed and not always_read:
        return {"buildPerformed": True, "build_action": action,
                "failedProjects": 0, "errorCount": 0, "errors": [],
                "warnings": [], "errorsRead": False,
                "errorSource": "not-needed", "message": "Build succeeded"}

    all_items, source, read_attempts = _read_build_errors(dte, failed)
    errors = [
        ({**item, "severity": "error"} if item.get("severity") != "error" else item)
        for item in all_items if _is_build_error(item, failed)
    ]
    error_source_items = all_items[:]
    warnings = [
        item for item in error_source_items
        if not _is_build_error(item, failed) and item["severity"] == "warning"
    ]
    result = {"buildPerformed": True, "build_action": action,
              "failedProjects": failed, "errorCount": len(errors),
              "errors": errors, "warnings": warnings,
              "errorsRead": bool(all_items), "errorSource": source,
              "errorReadAttempts": read_attempts,
              "diagnosticsPending": bool(failed and not errors)}
    if failed and not all_items:
        result["message"] = (
            "Build failed, but XAE did not expose diagnostics after 5 COM reads; "
            "diagnosticsPending=true. Retry plc build or use the embedded XAE Agent panel."
        )
    elif failed and not errors:
        result["message"] = (
            "Build failed, but no compiler error was exposed in the returned Error List; "
            "diagnosticsPending=true."
        )
    return result


def _project_info(dte) -> dict:
    if not str(dte.Solution.FullName or '') and int(dte.Solution.Projects.Count) == 0:
        return {'solution': '', 'project_count': 0, 'plc_projects': [],
                'state': 'connected_no_project'}
    info = tc_platform.get_project_info()
    if "solution" not in info:
        info["solution"] = str(dte.Solution.FullName or "")
    return info


def _connect_check(dte, preferred_pid: int = 0) -> dict:
    # MainWindow is not implemented by every TwinCAT DTE Automation dispatch
    # (notably some 4024/4026 shells).  Solution access remains usable, so
    # treat the window/PID as optional and retain the panel's requested PID.
    hwnd = _com._dte_hwnd(dte)
    try:
        solution = str(dte.Solution.FullName or "")
    except Exception:
        solution = ""
    try:
        name = str(dte.Name or "")
    except Exception:
        name = ""
    active_document = {}
    try:
        document = dte.ActiveDocument
        if document is not None:
            active_document = {
                "name": str(document.Name or ""),
                "full_name": str(document.FullName or ""),
                "kind": str(document.Kind or ""),
                "saved": bool(document.Saved),
            }
    except Exception:
        pass
    return {"solution": solution, "name": name,
            "solution_open": bool(getattr(dte.Solution, 'IsOpen', solution)),
            "pid": _window_pid(hwnd) or int(preferred_pid or 0),
            "bridge": "native-python-com", "active_document": active_document}


def _close_solution(dte) -> dict:
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    try:
        if int(dte.Solution.Projects.Count) == 0:
            return {"status": "skipped", "reason": "no solution open"}
    except Exception:
        pass
    dte.Solution.Close()
    return {"status": "closed"}


def _open_solution(dte, path: str) -> dict:
    solution = Path(path).expanduser().resolve()
    if not solution.is_file() or solution.suffix.lower() != ".sln":
        raise FileNotFoundError(f"Solution not found: {solution}")
    _close_solution(dte)
    dte.Solution.Open(str(solution))
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            if int(dte.Solution.Projects.Count) > 0:
                return {"status": "opened", "path": str(solution)}
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Solution did not finish loading: {solution}")


def _open_created_solution(dte, path):
    if bool(dte.Solution.IsOpen) or str(dte.Solution.FullName or '') or int(dte.Solution.Projects.Count):
        raise RuntimeError('XAE 已打开工程，拒绝用新建工程替换；生成文件已保留')
    return _open_solution(dte, path)


def _config_restart_command_names(dte) -> list[str]:
    """Return available XAE commands that restart TwinCAT in Config mode.

    The canonical command name changed between XAE releases.  In particular,
    some 4026 installations reject ``TwinCAT.RestartTwinCATConfigMode`` even
    though the corresponding toolbar button is present.  Querying the DTE
    command collection keeps the bridge tied to the currently running XAE.
    """
    names = ["TwinCAT.RestartTwinCATConfigMode"]
    try:
        commands = dte.Commands
        count = int(commands.Count)
        discovered = []
        for index in range(1, count + 1):
            try:
                name = str(commands.Item(index).Name or "")
            except Exception:
                continue
            folded = name.casefold()
            if ("twincat" in folded and "config" in folded
                    and ("restart" in folded or "start" in folded)):
                discovered.append(name)
        names.extend(sorted(discovered, key=lambda value: (
            0 if value == "TwinCAT.RestartTwinCATConfigMode" else 1,
            value.casefold(),
        )))
    except Exception:
        # DTE command enumeration is optional; retain the known legacy name
        # so older XAE versions keep working.
        pass
    return list(dict.fromkeys(names))


def _restart_config_mode_command(dte) -> str:
    errors = []
    for command_name in _config_restart_command_names(dte):
        try:
            retry_com_busy(lambda: dte.ExecuteCommand(command_name),
                           attempts=3, max_delay=1.0)
            return command_name
        except Exception as exc:
            errors.append(f"{command_name}: {exc}")
    # The XAE toolbar control is present on several 4026 installations even
    # when its DTE command alias is absent.  Execute that control directly;
    # this is the programmatic equivalent of clicking "Restart TwinCAT
    # (Config Mode)" and does not require bringing XAE to the foreground.
    try:
        for bar_index in range(1, int(dte.CommandBars.Count) + 1):
            try:
                bar = dte.CommandBars.Item(bar_index)
                controls = bar.Controls
            except Exception:
                continue
            stack = [controls]
            while stack:
                group = stack.pop()
                try:
                    count = int(group.Count)
                except Exception:
                    continue
                for control_index in range(1, count + 1):
                    try:
                        control = group.Item(control_index)
                        caption = str(getattr(control, "Caption", "") or "")
                        tooltip = str(getattr(control, "TooltipText", "") or "")
                    except Exception:
                        continue
                    label = f"{caption} {tooltip}".casefold()
                    if ("config" in label or "配置模式" in label) and (
                            "restart" in label or "重启" in label):
                        try:
                            control.Execute()
                            return f"XAE toolbar: {caption or tooltip}"
                        except Exception as exc:
                            errors.append(f"toolbar {caption or tooltip}: {exc}")
                    try:
                        children = control.Controls
                        if int(children.Count) > 0:
                            stack.append(children)
                    except Exception:
                        pass
    except Exception as exc:
        errors.append(f"toolbar discovery: {exc}")
    detail = "; ".join(errors) or "no matching DTE command was found"
    raise RuntimeError(
        "No usable XAE command to restart TwinCAT in Config mode. " + detail)


def _set_config_mode(dte, args: dict) -> dict:
    strategy = str(args.get("strategy") or "consume").lower()
    if strategy == "consume":
        result = tc_platform.set_config_mode()
        return {"strategy": "TIRS ConsumeXml", "result": result}
    if strategy == "command":
        command_name = _restart_config_mode_command(dte)
        return {"strategy": command_name, "requested": "Config"}
    raise ValueError(f"Unsupported config-mode strategy: {strategy}")


def _realtime_refresh(dte) -> dict:
    """Refresh the active XAE Real-Time designer without selecting a page."""
    names = ["TwinCAT.刷新", "TwinCAT.Refresh"]
    try:
        commands = dte.Commands
        for index in range(1, int(commands.Count) + 1):
            try:
                name = str(commands.Item(index).Name or "")
            except Exception:
                continue
            if re.match(r"(?i)^TwinCAT\.(刷新|Refresh)$", name) and name not in names:
                names.append(name)
    except Exception:
        pass
    errors = []
    for name in names:
        try:
            retry_com_busy(lambda name=name: dte.ExecuteCommand(name), attempts=3, max_delay=1.0)
            return {
                "status": "refreshed",
                "command": name,
                "focus_changed": False,
                "configuration_changed": False,
                "note": "仅刷新当前已打开的 XAE Real-Time 页面；未写配置、未激活、未重启。",
            }
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    detail = "; ".join(errors) or "no usable TwinCAT refresh command was found"
    raise RuntimeError(
        "Realtime 页面未刷新。请先在 XAE 中打开并选中 SYSTEM > Real-Time > Settings；"
        "本工具不会自动切换页面或抢焦点。 " + detail
    )


def _system_remove_and_save(dte, args: dict) -> dict:
    """Delete one SYSTEM node and persist the XAE project change."""
    result = tc_platform.system_remove(
        str(args.get("path") or ""), bool(args.get("apply", False)),
        bool(args.get("allow_with_children", False)), sysman=_system_manager(dte))
    if result.get("applied"):
        try:
            retry_com_busy(lambda: dte.ExecuteCommand("File.SaveAll"))
        except Exception as exc:
            raise RuntimeError(
                f"SYSTEM node '{result.get('path', args.get('path', ''))}' was removed "
                f"from the live XAE tree, but File.SaveAll failed: {exc}") from exc
        result = {**result, "saved": True}
    return result


def _dispatch_connected(command: str, args: dict, dte, preferred_pid: int = 0):
    simple = {
        "hmi-create-project": lambda: create_hmi_project(dte, args),
        "connect-check": lambda: _connect_check(dte, preferred_pid),
        "list": lambda: [
            {k: entry[k] for k in ("name", "folder", "itemType", "path", "parent", "depth")}
            for entry in _iter_objects(_system_manager(dte)) if entry["itemType"] != 601
        ],
        "read-pou": lambda: _read_pou(dte, args),
        "read-current": lambda: _read_current(dte, args),
        "read-batch": lambda: _read_batch(dte, args),
        "write-pou": lambda: _write_pou(dte, args),
        "patch-pou": lambda: _patch_pou(dte, args),
        "find-pou": lambda: _find_pou(dte, args),
        "search-code": lambda: _search_code(dte, args),
        "structure": lambda: _structure(dte, args),
        "solution-tree": lambda: _solution_tree(dte, args),
        "code-inventory": lambda: _code_inventory_command(dte, args),
        "all-code": lambda: [
            {"name": obj["name"], "folder": obj["folder"],
             "declaration": obj["declaration"].get("text", ""),
             "implementation": obj["implementation"].get("text", ""),
             "methods": obj["members"]}
            for obj in _code_inventory(dte, {"include_code": True})["objects"]
        ],
        "build-state": lambda: _build_state(dte, args),
        "member-baseline": lambda: _read_member_baseline(dte, args),
        "document-baseline": lambda: _save_document(dte, args),
        "save-document": lambda: _save_document(dte, args, save=True),
        "diagnostics": lambda: _diagnostics(dte),
        "build": lambda: _build(dte, args),
        "new-pou": lambda: plc.create_pou(
            str(args.get("type") or "fb"), str(args.get("name") or ""),
            declaration=str(args.get("declaration") or ""),
            implementation=str(args.get("implementation") or ""),
            language=str(args.get("language") or "ST"),
            return_type=str(args.get("return_type") or ""),
            parent_path=str(args.get("path") or args.get("parent_path") or "")),
        "new-folder": lambda: _new_folder(dte, args),
        "new-member": lambda: _new_member(dte, args),
        "delete-member": lambda: _delete_member(dte, args),
        "delete-pou": lambda: _delete_object(dte, args),
        "rename": lambda: _rename_object(dte, args),
        "rename-member": lambda: _rename_member(dte, args),
        "create-plc-project": lambda: plc.com_create_plc_project(
            str(args.get("name") or "PLC1"), str(args.get("template") or "Standard PLC Template")),
        "remove-plc-project": lambda: plc.com_remove_plc_project(str(args.get("name") or "")),
        "delete-plc-project": lambda: plc.com_delete_plc_project(str(args.get("name") or "")),
        "projects": tc_platform.list_projects,
        "project-info": lambda: _project_info(dte),
        "system-structure": lambda: tc_platform.system_structure(
            int(args.get("max_depth") or 6), list(args.get("roots") or []) or None,
            sysman=_system_manager(dte)),
        "system-settings": lambda: tc_platform.system_settings(
            sysman=_system_manager(dte)),
        "system-settings-set": lambda: tc_platform.system_settings_set(
            dict(args.get("settings") or {}), bool(args.get("apply", False)),
            sysman=_system_manager(dte)),
        "core-info": lambda: tc_platform.core_info(sysman=_system_manager(dte)),
        "realtime-info": lambda: tc_platform.realtime_info(sysman=_system_manager(dte)),
        "core-assign": lambda: tc_platform.core_assign(
            list(args.get("cpu_ids") or []), args.get("max_cpus"),
            args.get("affinity"), args.get("p_core_affinity"),
            args.get("e_core_affinity"),
            bool(args.get("apply", False)), sysman=_system_manager(dte)),
        "task-info": lambda: tc_platform.task_info(
            sysman=_system_manager(dte), max_depth=int(args.get("max_depth") or 6)),
        "task-core-assign": lambda: tc_platform.task_core_assign(
            str(args.get("task_path") or ""), int(args.get("cpu_id")),
            bool(args.get("apply", False)), sysman=_system_manager(dte)),
        "task-settings-set": lambda: tc_platform.task_settings_set(
            str(args.get("task_path") or ""), dict(args.get("settings") or {}),
            bool(args.get("apply", False)), sysman=_system_manager(dte)),
        "system-add": lambda: tc_platform.system_add(
            str(args.get("parent_path") or ""), str(args.get("name") or ""),
            int(args.get("item_type")), str(args.get("info") or ""),
            bool(args.get("apply", False)), sysman=_system_manager(dte)),
        "system-remove": lambda: _system_remove_and_save(dte, args),
        "realtime-refresh": lambda: _realtime_refresh(dte),
        "io-masters": tc_platform.get_io_masters,
        "io-master-info": lambda: tc_platform.get_io_master_info(str(args.get("master") or "")),
        "target-show": lambda: {"target_netid": str(tc_platform.get_target_net_id()),
                                  "solution": str(dte.Solution.FullName or "")},
        "target-set": lambda: tc_platform.set_target_net_id(str(args.get("netid") or "")),
        "io-scan": tc_platform.scan_devices,
        "close-solution": lambda: _close_solution(dte),
        "open-solution": lambda: _open_solution(dte, str(args.get("path") or "")),
        "open-created-solution": lambda: _open_created_solution(dte, str(args.get("path") or "")),
        "import-plcopen": lambda: plc.import_plcopen(str(args.get("file") or ""),
                                                     int(args.get("options") or 0)),
        "export-plcopen": lambda: plc.export_plcopen(str(args.get("file") or ""),
                                                     list(args.get("pous") or [])),
        "lib-list": plc.list_libraries,
        "library-evidence": lambda: _library_evidence(dte, args),
        "library-signatures": lambda: _library_signatures(dte, args),
        "compiler-settings": lambda: _compiler_settings(dte, args),
        "lib-scan": plc.scan_installed_libraries,
        "lib-add": lambda: plc.com_add_library(str(args.get("name") or ""),
                                                str(args.get("version") or "*"),
                                                str(args.get("distributor") or "")),
        "lib-remove": lambda: plc.com_remove_library(str(args.get("name") or ""),
                                                      str(args.get("version") or ""),
                                                      str(args.get("distributor") or "")),
        "placeholder-add": lambda: plc.com_add_placeholder(
            str(args.get("name") or ""), str(args.get("default_lib") or ""),
            str(args.get("default_version") or "*"), str(args.get("default_distributor") or "")),
        "placeholder-freeze": lambda: plc.com_freeze_placeholder(str(args.get("name") or "")),
        "repo-insert": lambda: plc.com_insert_repository(str(args.get("name") or ""),
                                                         str(args.get("root_folder") or ""),
                                                         int(args.get("index") or 0)),
        "repo-remove": lambda: plc.com_remove_repository(str(args.get("name") or "")),
        "lib-install": lambda: plc.com_install_library(str(args.get("repository") or ""),
                                                       str(args.get("lib_path") or ""),
                                                       bool(args.get("overwrite", False))),
        "lib-uninstall": lambda: plc.com_uninstall_library(
            str(args.get("repository") or ""), str(args.get("library") or ""),
            str(args.get("version") or ""), str(args.get("distributor") or "")),
        "state": tc_platform.get_runtime_state,
        "plc-runtimes": tc_platform.list_plc_runtimes,
        "plc-online-state": lambda: tc_platform.plc_online_state(str(args.get('runtime') or '')),
        "activate": tc_platform.activate_configuration,
        "restart": tc_platform.restart_twincat,
        "login": lambda: tc_platform.login(str(args.get('runtime') or ''), bool(args.get('all_plcs', False))),
        "logout": lambda: tc_platform.logout(str(args.get('runtime') or ''), bool(args.get('all_plcs', False))),
        "start": lambda: tc_platform.start_plc(str(args.get('runtime') or ''), bool(args.get('all_plcs', False))),
        "stop": lambda: tc_platform.stop_plc(str(args.get('runtime') or ''), bool(args.get('all_plcs', False))),
        "online": lambda: tc_platform.full_online_cycle(str(args.get('runtime') or ''), bool(args.get('all_plcs', False))),
        "config-mode": lambda: _set_config_mode(dte, args),
        "run-mode": tc_platform.set_run_mode,
        "nc-structure": lambda: tc_platform.nc_structure(int(args.get("max_depth") or 6)),
        "nc-axis-info": lambda: tc_platform.nc_axis_info(str(args.get("axis") or "")),
        "nc-axis-params": lambda: tc_platform.nc_axis_params(str(args.get("axis") or "")),
        "nc-axis-params-set": lambda: tc_platform.nc_set_axis_params(
            str(args.get("axis") or ""), dict(args.get("parameters") or {})),
        "nc-drive-list": tc_platform.find_servo_drives,
        "nc-encoder-list": tc_platform.find_nc_encoders,
        "nc-links": tc_platform.nc_links,
        "nc-state": tc_platform.nc_state,
        "nc-axis-state": lambda: tc_platform.nc_axis_state(str(args.get("axis") or "")),
        "nc-axis-move": lambda: tc_platform.nc_axis_move(
            str(args.get("axis") or ""), float(args.get("position") or 0),
            float(args.get("velocity") or 0), float(args.get("move_timeout") or 30),
            str(args.get("control_scope") or ""), bool(args.get("confirm_physical", False)),
            bool(args.get("require_homed", True))),
        "nc-validate": lambda: tc_platform.nc_validate(dict(args.get("configuration") or {})),
        "nc-create-task": lambda: tc_platform.nc_create_task(str(args.get("name") or "NC-Task")),
        "nc-create-axis": lambda: tc_platform.nc_create_axis(
            str(args.get("task") or ""), str(args.get("name") or ""),
            str(args.get("axis_type") or "continuous")),
        "nc-link-drive": lambda: tc_platform.nc_link_drive(
            str(args.get("axis") or ""), str(args.get("drive_path") or ""),
            args.get("channel")),
        "nc-quick-link": lambda: tc_platform.nc_quick_link(
            str(args.get("axis") or ""), str(args.get("drive_path") or "")),
        "nc-link-encoder": lambda: tc_platform.nc_link_encoder(
            str(args.get("axis") or ""), str(args.get("encoder_path") or "")),
    }
    handler = simple.get(command)
    if handler is None:
        raise ValueError(f"Native COM bridge does not support command '{command}'")
    retryable = {
        "connect-check", "list", "read-pou", "read-current", "read-batch", "write-pou", "find-pou",
        "search-code", "structure", "code-inventory", "all-code", "build-state", "build",
        "projects", "project-info", "target-show", "lib-list", "lib-scan",
        "state",
        "system-structure", "system-settings", "core-info", "realtime-info", "task-info",
        "nc-structure", "nc-axis-info", "nc-axis-params", "nc-drive-list",
        "nc-encoder-list", "nc-links", "nc-state", "nc-axis-state", "nc-validate",
    }
    return retry_com_busy(handler) if command in retryable else handler()


def dispatch(command: str, args: dict | None = None):
    """Execute one TcCom-compatible command without launching PowerShell."""
    payload = dict(args or {})
    prefer_pid = int(payload.pop("preferPid", 0) or 0)
    strict_pid = bool(payload.pop("strictPid", False))
    payload.pop("sticky", None)
    with com_apartment(), target_process(prefer_pid):
        dte = retry_com_busy(
            lambda: get_active_dte(prefer_pid=prefer_pid, strict_pid=strict_pid)
        )
        silent_state = (
            _begin_scoped_silent_mode(dte)
            if command in _SCOPED_SILENT_COMMANDS else None
        )
        try:
            return _dispatch_connected(command, payload, dte, prefer_pid)
        finally:
            _restore_scoped_silent_mode(silent_state)


def available() -> tuple[bool, str]:
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
        return True, ""
    except Exception as exc:
        return False, str(exc)
