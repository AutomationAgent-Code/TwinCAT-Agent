"""PowerShell-free manifest-driven EtherCAT I/O configuration."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ._com import com_apartment, get_active_dte, target_process


META_NAMES = {
    "Image", "Image-Info", "Process Image", "Process Image-Info",
    "Inputs", "Outputs", "InfoData", "SyncUnits", "<default>",
}


def load_manifest(path: str = "", configuration: dict | None = None) -> dict:
    if configuration is not None:
        return dict(configuration)
    manifest = Path(path).expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest}")
    if manifest.suffix.lower() != ".json":
        raise ValueError("Only JSON manifests are supported")
    return json.loads(manifest.read_text(encoding="utf-8-sig"))


def check_manifest(data: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if int(data.get("schema_version") or 0) != 1:
        errors.append("schema_version must be 1.")
    master = data.get("master") or {}
    if not str(master.get("name") or ""):
        errors.append("master.name is required.")
    devices = data.get("devices")
    if not isinstance(devices, list):
        errors.append("devices array is required.")
        devices = []
    ids: set[str] = set()
    names: set[str] = set()
    for device in devices:
        device_id = str(device.get("id") or "")
        name = str(device.get("name") or "")
        parent = str(device.get("parent") or "")
        products = list(device.get("product_candidates") or [])
        if not device_id:
            errors.append(f"Device '{name}' has no id.")
            continue
        if device_id in ids:
            errors.append(f"Duplicate device id: {device_id}")
        ids.add(device_id)
        if not name:
            errors.append(f"Device '{device_id}' has no name.")
        elif name in names:
            warnings.append(f"Duplicate display name '{name}'; ids remain authoritative.")
        names.add(name)
        if not parent:
            errors.append(f"Device '{device_id}' has no parent.")
        if not products or not str(products[0] or ""):
            errors.append(f"Device '{device_id}' has no product_candidates.")
        if device.get("subtype") is not None and int(device["subtype"]) < 1:
            errors.append(f"Device '{device_id}' has invalid subtype.")
    for device in devices:
        parent = str(device.get("parent") or "")
        before = str(device.get("before") or "")
        if parent != "$master" and parent not in ids:
            errors.append(f"Device '{device.get('id')}' references unknown parent '{parent}'.")
        if before and before not in ids:
            errors.append(f"Device '{device.get('id')}' references unknown before id '{before}'.")
    seen = {"$master"}
    for device in devices:
        parent = str(device.get("parent") or "")
        if parent not in seen:
            errors.append(f"Parent '{parent}' must appear before child '{device.get('id')}'.")
        seen.add(str(device.get("id") or ""))
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "device_count": len(devices)}


def _esi_roots(data: dict) -> list[Path]:
    candidates = [Path(str(item)) for item in (data.get("esi_roots") or [])]
    candidates += [
        Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT"),
        Path(r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT"),
        Path(r"C:\TwinCAT\3.1\Config\Io\EtherCAT"),
    ]
    result = []
    for candidate in candidates:
        if candidate.is_dir() and candidate.resolve() not in result:
            result.append(candidate.resolve())
    return result


def check_esi(data: dict) -> dict:
    roots = _esi_roots(data)
    files = [path for root in roots for path in root.glob("*.xml")]
    cache: dict[Path, str] = {}
    found, missing = [], []
    for device in data.get("devices") or []:
        hits = []
        for product in device.get("product_candidates") or []:
            needle = str(product)
            for path in files:
                if path not in cache:
                    try:
                        cache[path] = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        cache[path] = ""
                if needle in cache[path]:
                    hits.append({"product": needle, "file": str(path)})
        unique = list({(hit["product"], hit["file"]): hit for hit in hits}.values())
        record = {"id": str(device.get("id") or ""),
                  "name": str(device.get("name") or ""), "matches": unique}
        (found if unique else missing).append(record)
    return {"roots": [str(root) for root in roots], "esi_file_count": len(files),
            "found": found, "missing": missing, "ok": not missing}


def _sysman(dte):
    for index in range(1, int(dte.Solution.Projects.Count) + 1):
        try:
            candidate = dte.Solution.Projects.Item(index).Object
            candidate.LookupTreeItem("TIID")
            return candidate
        except Exception:
            continue
    raise RuntimeError("TwinCAT System Manager project not found in current solution")


def _children(parent) -> list:
    try:
        return list(parent or [])
    except Exception:
        return []


def _name(item) -> str:
    try:
        return str(item.Name or "")
    except Exception:
        return ""


def _find_child(parent, name: str):
    return next((child for child in _children(parent) if _name(child) == name), None)


def _physical_children(parent) -> list:
    return [child for child in _children(parent) if _name(child) not in META_NAMES]


def _item_type(item) -> int:
    try:
        return int(item.ItemType)
    except Exception:
        return -1


def _io_masters(io_root) -> list:
    """Return real System Manager I/O devices, never process-image folders."""
    return [child for child in _children(io_root) if _item_type(child) == 2]


def create_configuration(data: dict, dte, allow_existing: bool = False) -> dict:
    io_root = _sysman(dte).LookupTreeItem("TIID")
    created, skipped, failed = [], [], []
    nodes: dict[str, Any] = {}
    master_data = data["master"]
    master_name = str(master_data["name"])
    master = _find_child(io_root, master_name)
    if master is None:
        # TIID may expose process-image/meta nodes even when the configured
        # I/O device count is zero.  Only ItemType=2 devices make the tree
        # non-empty for creation safety purposes.
        if _io_masters(io_root) and not allow_existing:
            raise RuntimeError("I/O tree is not empty. Use allow_existing only after reviewing it.")
        master = io_root.CreateChild(master_name, int(master_data.get("subtype") or 111), None, None)
        created.append({"id": "$master", "name": master_name, "product": "EtherCAT Master"})
    else:
        skipped.append({"id": "$master", "name": master_name})
    nodes["$master"] = master
    by_id = {str(device["id"]): device for device in data.get("devices") or []}
    for device in data.get("devices") or []:
        device_id = str(device["id"])
        name = str(device["name"])
        parent_id = str(device["parent"])
        parent = nodes.get(parent_id)
        if parent is None:
            failed.append({"id": device_id, "name": name,
                           "error": f"Parent '{parent_id}' unavailable."})
            continue
        existing = _find_child(parent, name)
        if existing is not None:
            nodes[device_id] = existing
            skipped.append({"id": device_id, "name": name})
            continue
        before_name = ""
        before_id = str(device.get("before") or "")
        if before_id and before_id in by_id:
            candidate_name = str(by_id[before_id]["name"])
            if _find_child(parent, candidate_name) is not None:
                before_name = candidate_name
        subtype = int(device.get("subtype") or 9099)
        errors = []
        for product in device.get("product_candidates") or []:
            try:
                node = parent.CreateChild(name, subtype, before_name, str(product))
                nodes[device_id] = node
                created.append({"id": device_id, "name": name,
                                "product": str(product), "subtype": subtype})
                break
            except Exception as exc:
                errors.append(f"{product}: {exc}")
        if device_id not in nodes:
            failed.append({"id": device_id, "name": name, "error": " | ".join(errors)})
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    return {"solution": str(dte.Solution.FullName or ""), "created": created,
            "skipped": skipped, "failed": failed, "activated": False, "scanned": False}


def validate_configuration(data: dict, dte) -> dict:
    io_root = _sysman(dte).LookupTreeItem("TIID")
    nodes: dict[str, Any] = {}
    missing, present, order_mismatches = [], [], []
    master = next((item for item in _io_masters(io_root)
                   if _name(item) == str(data["master"]["name"])), None)
    if master is None:
        return {"ok": False, "missing": ["$master"], "present": [], "order_mismatches": []}
    nodes["$master"] = master
    for device in data.get("devices") or []:
        device_id = str(device["id"])
        parent = nodes.get(str(device["parent"]))
        node = _find_child(parent, str(device["name"])) if parent is not None else None
        if node is None:
            missing.append(device_id)
        else:
            nodes[device_id] = node
            present.append(device_id)
    groups: dict[str, list[dict]] = {}
    for device in data.get("devices") or []:
        groups.setdefault(str(device["parent"]), []).append(device)
    for parent_id, devices in groups.items():
        parent = nodes.get(parent_id)
        if parent is None:
            continue
        actual = [_name(item) for item in _physical_children(parent)]
        expected = [str(device["name"]) for device in devices]
        actual_relevant = [name for name in actual if name in expected]
        expected_present = [name for name in expected if name in actual]
        if actual_relevant != expected_present:
            order_mismatches.append({"parent": parent_id, "expected": expected_present,
                                     "actual": actual_relevant})
    return {"ok": not missing and not order_mismatches, "missing": missing,
            "present": present, "order_mismatches": order_mismatches}


def _export_master(master) -> dict:
    devices, used_ids = [], set()

    def make_id(name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "device"
        candidate, number = base, 2
        while candidate in used_ids:
            candidate, number = f"{base}-{number}", number + 1
        used_ids.add(candidate)
        return candidate

    def walk(parent, parent_id: str) -> None:
        for child in _physical_children(parent):
            name = _name(child)
            device_id = make_id(name)
            product = ""
            try:
                root = ET.fromstring(str(child.ProduceXml() or ""))
                node = root.find(".//ProductRevision")
                product = str(node.text or "") if node is not None else ""
            except Exception:
                pass
            if not product:
                try:
                    product = str(child.ItemSubTypeName or "")
                except Exception:
                    product = ""
            try:
                subtype = int(child.ItemSubType)
            except Exception:
                subtype = 9099
            devices.append({"id": device_id, "name": name, "parent": parent_id,
                            "subtype": subtype, "product_candidates": [product]})
            walk(child, device_id)

    walk(master, "$master")
    try:
        master_subtype = int(master.ItemSubType)
    except Exception:
        master_subtype = 111
    return {"schema_version": 1,
            "master": {"name": _name(master), "subtype": master_subtype},
            "devices": devices,
            "policy": {"scan": False, "activate": False, "restart": False}}


def export_configuration(dte, master_name: str = "", allow_multiple: bool = False) -> dict:
    io_root = _sysman(dte).LookupTreeItem("TIID")
    masters = _io_masters(io_root)
    available = [_name(item) for item in masters]
    if master_name:
        selected = next((item for item in masters if _name(item) == master_name), None)
        if selected is None:
            raise RuntimeError(
                f"I/O master '{master_name}' was not found. Available masters: {available}")
        return _export_master(selected)
    if not masters:
        return {"master_count": 0, "masters": [], "empty": True,
                "message": "No configured I/O master"}
    if len(masters) == 1:
        return _export_master(masters[0])
    if allow_multiple:
        return {"master_count": len(masters),
                "masters": [_export_master(item) for item in masters]}
    raise RuntimeError(
        "Multiple I/O masters are configured; pass the exact master name. "
        f"Available masters: {available}")


def remove_configuration(data: dict, dte, confirmed: bool = False) -> dict:
    if not confirmed:
        raise PermissionError("Removal requires explicit confirmation")
    io_root = _sysman(dte).LookupTreeItem("TIID")
    master = next((item for item in _io_masters(io_root)
                   if _name(item) == str(data["master"]["name"])), None)
    if master is None:
        return {"removed": [], "missing": ["$master"], "activated": False}
    nodes: dict[str, Any] = {"$master": master}
    for device in data.get("devices") or []:
        parent = nodes.get(str(device["parent"]))
        child = _find_child(parent, str(device["name"])) if parent is not None else None
        if child is not None:
            nodes[str(device["id"])] = child
    removed, missing = [], []
    for device in reversed(data.get("devices") or []):
        parent = nodes.get(str(device["parent"]))
        child = _find_child(parent, str(device["name"])) if parent is not None else None
        if child is None:
            missing.append(str(device["id"]))
            continue
        parent.DeleteChild(str(device["name"]))
        removed.append(str(device["id"]))
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    return {"removed": removed, "missing": missing, "master_preserved": True,
            "activated": False}


def remove_master(master_name: str, dte, confirmed: bool = False,
                  allow_with_children: bool = False) -> dict:
    if not confirmed:
        raise PermissionError("Removing an I/O master requires explicit confirmation")
    name = str(master_name or "").strip()
    if not name:
        raise ValueError("master name is required")
    sysman = _sysman(dte)
    io_root = sysman.LookupTreeItem("TIID")
    master = next((item for item in _io_masters(io_root) if _name(item) == name), None)
    if master is None:
        raise FileNotFoundError("I/O master not found: " + name)
    children = _physical_children(master)
    if children and not allow_with_children:
        raise RuntimeError(
            f"I/O master '{name}' still has {len(children)} physical child device(s); "
            "remove them first or explicitly allow_with_children."
        )
    io_root.DeleteChild(name)
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    return {"removed": name, "child_count": len(children),
            "allow_with_children": bool(allow_with_children)}


def execute(command: str, *, manifest: str = "", configuration: dict | None = None,
            output: str = "", allow_existing: bool = False,
            confirm_remove: bool = False, prefer_pid: int = 0,
            master: str = "", allow_with_children: bool = False) -> dict:
    needs_manifest = command in {"check-manifest", "esi-check", "create", "validate", "remove"}
    data = load_manifest(manifest, configuration) if needs_manifest else None
    validation = check_manifest(data) if data is not None else None
    if validation is not None and not validation["ok"]:
        return {"command": command, "status": "invalid-manifest", "validation": validation}
    if command == "check-manifest":
        return {"command": command, "status": "ok", "validation": validation}
    if command == "esi-check":
        return {"command": command, "status": "ok", "result": check_esi(data)}
    with com_apartment(), target_process(prefer_pid):
        dte = get_active_dte(prefer_pid=prefer_pid, strict_pid=prefer_pid > 0)
        if command == "create":
            result = create_configuration(data, dte, allow_existing)
        elif command == "validate":
            result = validate_configuration(data, dte)
        elif command == "export":
            result = export_configuration(dte, master, allow_multiple=not bool(output))
            if output and not result.get("empty"):
                destination = Path(output).expanduser().resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            elif output and result.get("empty"):
                result["output_skipped"] = output
        elif command == "remove":
            result = remove_configuration(data, dte, confirm_remove)
        elif command == "remove-master":
            result = remove_master(master, dte, confirm_remove, allow_with_children)
        else:
            raise ValueError(f"Unsupported I/O command: {command}")
    status = "empty" if isinstance(result, dict) and result.get("empty") else "ok"
    return {"command": command, "status": status, "result": result}
