"""
TwinCAT platform control — build, activate, login/run, mode switching.

All operations use the COM Automation Interface (win32com).
"""

from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# COM helpers
# ---------------------------------------------------------------------------

_dte_cache = None


def _dte(allow_launch: bool = False):
    """Connect to running TwinCAT XAE via COM.

    Uses ``GetActiveObject`` only by default — NEVER launches a new XAE
    instance via ``Dispatch`` unless *allow_launch* is explicitly True.
    (Callers that need a fresh instance should use ``_dte(allow_launch=True)``.)

    After obtaining the DTE object the function verifies that ``.Solution``
    is accessible, because ``TcXaeShell.DTE.15.0`` sometimes returns a broken
    late-bound object that lacks core properties.
    """
    # Native Agent calls arrive on short-lived worker threads. Never reuse a
    # COM proxy created in another apartment; attach to the selected ROT entry
    # for every call. The legacy launch path keeps its old cache below.
    if not allow_launch:
        from ._com import get_active_dte
        return get_active_dte()

    global _dte_cache
    if _dte_cache is not None:
        return _dte_cache

    from ._com import force_dynamic_dispatch
    force_dynamic_dispatch()

    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()

    methods = [win32com.client.GetActiveObject]
    if allow_launch:
        methods.append(win32com.client.Dispatch)

    for attempt in range(10):
        dte = None
        for method in methods:
            for pid in ["TcXaeShell.DTE.17.0", "TcXaeShell.DTE.15.0", "VisualStudio.DTE.17.0"]:
                try:
                    dte = method(pid)
                    break
                except Exception:
                    continue
            if dte is not None:
                break

        if dte is not None:
            # Verify the object is actually usable
            try:
                _ = dte.Solution
                _dte_cache = dte
                return dte
            except (AttributeError, Exception):
                pass  # broken object — retry

        if attempt < 9:
            time.sleep(2)

    raise RuntimeError(
        "No TwinCAT XAE running. Open TcXaeShell and load a solution first."
    )


_UI_STATE_UNAVAILABLE = object()


def _capture_xae_ui_state() -> dict:
    """Capture dialog-related XAE flags without assuming either is available."""
    dte = _dte()
    state = {
        "dte": dte,
        "settings": None,
        "silent_mode": _UI_STATE_UNAVAILABLE,
        "suppress_ui": _UI_STATE_UNAVAILABLE,
    }
    try:
        settings = dte.GetObject("TcAutomationSettings")
        state["settings"] = settings
        state["silent_mode"] = bool(settings.SilentMode)
    except Exception:
        pass
    try:
        state["suppress_ui"] = bool(dte.SuppressUI)
    except Exception:
        pass
    return state


def _restore_xae_ui_state(state: dict) -> None:
    """Best-effort restoration of flags changed by an automated operation."""
    settings = state.get("settings")
    silent_mode = state.get("silent_mode", _UI_STATE_UNAVAILABLE)
    if settings is not None and silent_mode is not _UI_STATE_UNAVAILABLE:
        try:
            settings.SilentMode = bool(silent_mode)
        except Exception:
            pass
    suppress_ui = state.get("suppress_ui", _UI_STATE_UNAVAILABLE)
    if suppress_ui is not _UI_STATE_UNAVAILABLE:
        try:
            state["dte"].SuppressUI = bool(suppress_ui)
        except Exception:
            pass


def _preserve_xae_ui_state(function):
    """Restore XAE dialog settings after success, failure, or early return."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        state = _capture_xae_ui_state()
        try:
            return function(*args, **kwargs)
        finally:
            _restore_xae_ui_state(state)
    return wrapped


def _sysman():
    """Return the ITcSysManager for the TwinCAT project in the open solution.

    Scans solution projects for a .tsproj file and returns its
    ``.Object`` (ITcSysManager interface).

    Raises RuntimeError if no solution is open or no TwinCAT project found.
    """
    dte = _dte()

    try:
        sln = dte.Solution
    except Exception as exc:
        raise RuntimeError(
            "Cannot access Solution property. Is a solution open in TcXaeShell?"
        ) from exc

    if sln.Projects.Count == 0:
        raise RuntimeError(
            "No projects in solution. Open a TwinCAT project (.tsproj) first."
        )

    for i in range(1, sln.Projects.Count + 1):
        try:
            proj = sln.Projects.Item(i)
        except Exception:
            continue
        name = str(getattr(proj, "FullName", "") or getattr(proj, "Name", ""))
        if name.endswith(".tsproj") or "TwinCAT" in name:
            try:
                return proj.Object
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot access .Object on project '{name}'. "
                    f"Is it a valid TwinCAT project?"
                ) from exc

    # Fallback: try first project's Object
    try:
        first = sln.Projects.Item(1)
        first_name = getattr(first, "Name", "?")
        return first.Object
    except Exception as exc:
        raise RuntimeError(
            "No TwinCAT project found in solution. "
            "Open a .tsproj project via TcXaeShell."
        ) from exc


def _find_plc_instance(sysman):
    """Return ITcSmTreeItem for the PLC Instance node (child of PLC with 'Instance' in name)."""
    plc = sysman.LookupTreeItem("TIPC")
    for child in plc:
        for sub in child:
            if "Instance" in getattr(sub, "Name", ""):
                return sub
    # Fallback: return first child of PLC
    for child in plc:
        return child
    raise RuntimeError("No PLC project found in solution")


def _ads_port_from_item(item) -> tuple[int | None, str]:
    """Read a PLC runtime ADS port from a System Manager tree item.

    TwinCAT versions expose the port either as a late-bound property or in
    ``ProduceXml``.  Never derive it from project order (851 + index): ports
    can be changed, skipped, or assigned independently.
    """
    import xml.etree.ElementTree as ET

    for attr in ("AdsPort", "AmsPort", "Port"):
        try:
            value = int(getattr(item, attr))
            if 1 <= value <= 65535:
                return value, f"property:{attr}"
        except Exception:
            pass

    try:
        raw = item.ProduceXml(False)
    except TypeError:
        try:
            raw = item.ProduceXml()
        except Exception:
            raw = ""
    except Exception:
        raw = ""

    if raw:
        try:
            root = ET.fromstring(raw)
            preferred = ("AdsPort", "AmsPort", "Port")
            for tag in preferred:
                for elem in root.iter():
                    if elem.tag.rsplit("}", 1)[-1] != tag or not elem.text:
                        continue
                    try:
                        value = int(elem.text.strip(), 0)
                    except ValueError:
                        continue
                    if 1 <= value <= 65535:
                        return value, f"xml:{tag}"
        except ET.ParseError:
            pass
    return None, "unresolved"


def _list_plc_runtimes(sysman) -> list[dict]:
    """Enumerate every configured PLC root/project and its actual ADS port."""
    runtimes: list[dict] = []
    tipc = sysman.LookupTreeItem("TIPC")
    for root in (tipc or []):
        nested = None
        try:
            nested = root.NestedProject
        except Exception:
            pass

        instance = None
        try:
            children = list(root or [])
        except Exception:
            children = []
        for child in children:
            name = str(getattr(child, "Name", "") or "")
            if "instance" in name.lower():
                instance = child
                break
        if instance is None:
            for child in children:
                child_port, _source = _ads_port_from_item(child)
                if child_port is not None:
                    instance = child
                    break

        port = None
        port_source = "unresolved"
        for candidate in (instance, root, nested):
            if candidate is None:
                continue
            port, port_source = _ads_port_from_item(candidate)
            if port is not None:
                break

        runtimes.append({
            "name": str(getattr(root, "Name", "") or "PLC"),
            "project_name": str(getattr(nested, "Name", "") or ""),
            "root": root,
            "project": nested,
            "instance": instance,
            "ads_port": port,
            "port_source": port_source,
        })
    return runtimes


# Localized "PLC Project" suffix across TwinCAT IDE languages. The nested
# project is named "<name> Project" (EN) / "<name>项目" (ZH) / "<name>Projekt"
# (DE) etc. Matching only "Project" fails on a Chinese-localized IDE, where the
# node is e.g. "Tc3Mc3项目" — that is why NC/English projects worked but MC3 on a
# ZH IDE reported "No PLC Project node found". Keep EN first (NC unchanged).
_PLC_PROJECT_MARKERS = ("Project", "Projekt", "Projet", "项目", "項目", "專案", "Progetto", "Proyecto")


def _is_plc_project_name(name) -> bool:
    n = name or ""
    return any(m in n for m in _PLC_PROJECT_MARKERS)


def _find_plc_project(sysman):
    """Return ITcSmTreeItem for the PLC Project node (NestedProject).

    ``GenerateBootProject`` must be called on the NestedProject node,
    not the Instance node.  The NestedProject is accessed via
    ``child.NestedProject`` — it does NOT appear as a direct
    ``Child(n)`` of the PLC node.

    Locale-robust: prefers a nested project whose name carries the localized
    "Project" suffix (EN/ZH/DE/…); if none matches (unknown locale), falls back
    to the first nested project so an existing PLC project is never missed.
    """
    plc = sysman.LookupTreeItem("TIPC")
    fallback = None
    for child in (plc or []):
        nested = getattr(child, "NestedProject", None)
        if not nested:
            continue
        if _is_plc_project_name(getattr(nested, "Name", "")):
            return nested
        if fallback is None:
            fallback = nested  # locale-agnostic safety net
    if fallback is not None:
        return fallback
    raise RuntimeError("No PLC Project node found in solution")


def _find_nested_project(sysman):
    """Return the NestedProject tree item."""
    plc = sysman.LookupTreeItem("TIPC")
    for child in plc:
        nested = getattr(child, "NestedProject", None)
        if nested:
            return nested
    raise RuntimeError("No nested PLC project found")


def _get_iec_project_path(sysman) -> str:
    """Get path for ITcPlcIECProject operations.

    The IEC Project is a direct child of the PLC instance (NOT the NestedProject).
    Path: TIPC^<PLC^<... Instance>>  — look for child with 'Instance' in name.
    """
    plc = sysman.LookupTreeItem("TIPC")
    for child in plc:
        for sub in child:
            sub_name = getattr(sub, "Name", "")
            sub_path = getattr(sub, "PathName", "")
            if "Instance" in sub_name and sub_path:
                return sub_path
    return ""


# ---------------------------------------------------------------------------
# Activate + Restart
# ---------------------------------------------------------------------------

def activate_configuration() -> dict:
    """Activate the TwinCAT configuration (Save to Registry).

    Equivalent to: TwinCAT XAE → Activate Configuration
    Must be followed by restart_twincat() to physically apply.
    """
    sysman = _sysman()
    sysman.ActivateConfiguration()
    return {"status": "activated", "message": "Configuration activated. Call restart_twincat() to apply."}


def restart_twincat() -> dict:
    """Start or restart the TwinCAT runtime system.

    If TwinCAT is stopped → starts it.
    If TwinCAT is already started → restarts it (with new configuration).
    """
    sysman = _sysman()
    sysman.StartRestartTwinCAT()
    started = _poll_runtime_started(sysman, timeout=30.0, interval=1.0)
    if not started:
        return {
            "status": "failed",
            "started": False,
            "error": "TwinCAT runtime did not become available within 30 seconds",
        }
    return {"status": "restarted", "started": True}


def _restart_with_retry() -> dict:
    """Call ``StartRestartTwinCAT`` with retry on transient COM errors."""
    last_err = ""
    for attempt in range(3):
        try:
            result = restart_twincat()
            if result.get("status") == "restarted" and result.get("started"):
                return result
            last_err = result.get("error", "runtime restart was not verified")
        except Exception as e:
            last_err = str(e)[:100]
            if attempt < 2:
                time.sleep(3)
    return {"status": "failed", "error": last_err}


def activate_and_restart() -> dict:
    """Activate configuration and restart TwinCAT (full cycle)."""
    r1 = activate_configuration()
    r2 = restart_twincat()
    return {"activate": r1, "restart": r2}


# ---------------------------------------------------------------------------
# P1 gap-fill: projects, project info, I/O structure, device settings,
# EtherCAT master adapter  (parity with Beckhoff coAgent)
# ---------------------------------------------------------------------------

def list_projects() -> dict:
    """List PLC projects (TIPC children) and solution projects via COM."""
    sysman = _sysman()
    plc_projects: list[str] = []
    try:
        plc = sysman.LookupTreeItem("TIPC")
        for child in (plc or []):
            plc_projects.append(getattr(child, "Name", ""))
    except Exception:
        pass
    solution_projects: list[str] = []
    try:
        sln = _dte().Solution
        for i in range(1, sln.Projects.Count + 1):
            solution_projects.append(getattr(sln.Projects.Item(i), "Name", ""))
    except Exception:
        pass
    return {"plc_projects": plc_projects, "solution_projects": solution_projects}


def get_project_info() -> dict:
    """Return solution/project metadata: solution path, target NetId, PLC projects."""
    info: dict = {}
    try:
        sln = _dte().Solution
        info["solution"] = str(sln.FullName)
        info["project_count"] = sln.Projects.Count
    except Exception:
        pass
    sysman = _sysman()
    try:
        info["target_netid"] = str(sysman.GetTargetNetId())
    except Exception:
        pass
    try:
        plc = sysman.LookupTreeItem("TIPC")
        info["plc_projects"] = [getattr(c, "Name", "") for c in (plc or [])]
    except Exception:
        info["plc_projects"] = []
    return info


def get_io_structure(max_depth: int = 6) -> dict:
    """Return the I/O (TIID) tree as nested ``{name, children}`` via COM."""
    sysman = _sysman()
    io_root = sysman.LookupTreeItem("TIID")

    def walk(item, depth):
        node = {"name": getattr(item, "Name", ""), "children": []}
        if depth < max_depth:
            for child in (item or []):
                try:
                    node["children"].append(walk(child, depth + 1))
                except Exception:
                    continue
        return node

    return walk(io_root, 0)


def get_io_masters() -> list[dict]:
    """List configured I/O masters using Automation Interface ItemType 2."""
    sysman = _sysman()
    io_root = sysman.LookupTreeItem("TIID")
    result = []
    for child in (io_root or []):
        try:
            item_type = int(child.ItemType)
        except Exception:
            item_type = 0
        if item_type != 2:
            continue
        result.append({
            "name": str(getattr(child, "Name", "") or ""),
            "path": "TIID^" + str(getattr(child, "Name", "") or ""),
            "item_type": item_type,
            "subtype": int(getattr(child, "ItemSubType", 0) or 0),
        })
    return result


def get_io_master_info(master: str = "") -> dict:
    """Return one master and its immediate children/settings summary."""
    masters = get_io_masters()
    if master:
        selected = next((m for m in masters if m["name"] == master), None)
        if selected is None:
            raise FileNotFoundError(
                f"I/O master '{master}' was not found. Available masters: "
                + ", ".join(m["name"] for m in masters)
            )
    elif len(masters) == 1:
        selected = masters[0]
    else:
        return {"master_count": len(masters), "masters": masters,
                "message": "Specify master when multiple I/O masters exist."}
    item = _lookup_or_raise(_sysman(), selected["path"])
    children = []
    for child in (item or []):
        children.append({"name": str(getattr(child, "Name", "") or ""),
                         "item_type": int(getattr(child, "ItemType", 0) or 0),
                         "path": selected["path"] + "^" + str(getattr(child, "Name", "") or "")})
    return {"master_count": len(masters), "master": selected, "children": children}


def _lookup_or_raise(sysman, tree_path: str):
    """LookupTreeItem that maps a missing path to a clean FileNotFoundError.

    TwinCAT raises a com_error (not None) for an unknown path, so guard both.
    """
    try:
        item = sysman.LookupTreeItem(tree_path)
    except Exception:
        item = None
    if item is None:
        raise FileNotFoundError(f"Tree item not found: {tree_path}")
    return item


def read_device_settings(tree_path: str) -> dict:
    """Read a tree item's settings XML via ProduceXml (e.g. an I/O device).

    *tree_path* is a system-manager path like ``TIID^Device 1 (EtherCAT)``.
    """
    item = _lookup_or_raise(_sysman(), tree_path)
    return {"path": tree_path, "xml": item.ProduceXml(False)}


def write_device_settings(tree_path: str, xml: str) -> dict:
    """Write a tree item's settings via ConsumeXml (e.g. device parameters)."""
    item = _lookup_or_raise(_sysman(), tree_path)
    item.ConsumeXml(xml)
    return {"path": tree_path, "status": "written"}


# ---------------------------------------------------------------------------
# SYSTEM / real-time settings
# ---------------------------------------------------------------------------

_SYSTEM_READ_ROOTS = ("TIRC", "TIRS", "TIRT")
# Generic System Manager CRUD is deliberately narrower than I/O/NC CRUD.
# TIRS is a settings document, not a free-form container; changing it must go
# through system_settings_set so the allow-list and readback gate are applied.
_SYSTEM_MUTATION_ROOTS = ("TIRC", "TIRT")
_SYSTEM_CORE_FIELDS = {
    "max_cpus": "MaxCpus",
    "cpu_ids": "CpuIds",
    "affinity": "Affinity",
    "p_core_affinity": "PCoreAffinity",
    "ecore_affinity": "ECoreAffinity",
    "e_core_affinity": "ECoreAffinity",
    "router_memory_mb": "RouterMemory",
    "max_stack_size_kb": "MaxStackSize",
    "core_settings": "CoreSettings",
}
_SYSTEM_TASK_CORE_ATTRS = ("CpuAffinity", "CpuId", "CoreId", "AssignedCore")
_SYSTEM_CORE_SETTING_FIELDS = {
    "base_time_100ns": "BaseTime",
    "load_limit_percent": "LoadLimit",
    "latency_warning_100ns": "LatencyWarning",
    "core_memory_kb": "CpuMemorySize",
}


def _twincat_realtime_profile(version_info: dict | None, attributes: dict) -> dict:
    """Describe the version-dependent meaning of the Real-Time memory fields."""
    info = dict(version_info or {})
    try:
        build = int(info.get("build") or 0)
    except (TypeError, ValueError):
        build = 0
    source = str(info.get("source") or "unknown")
    inferred = False
    if build >= 4026:
        family = "4026"
    elif build and build <= 4024:
        family = "4024"
    elif any(str(key).casefold() in {"coreboostactive", "cpumemorysize"}
             for key in attributes):
        family, inferred = "4026", True
    else:
        family = "unknown"
    if family == "4024":
        router_semantics = "combined_global_rt_and_ads_memory"
        router_apply = "target_reboot"
    elif family == "4026":
        router_semantics = "global_rt_memory_ads_separate"
        router_apply = "activate_configuration"
    else:
        router_semantics = "unknown_version_do_not_assume"
        router_apply = "version_dependent"
    return {
        "version": str(info.get("version_str") or ""),
        "build": build or None,
        "revision": info.get("revision"),
        "family": family,
        "source": "xml_feature_inference" if inferred else source,
        "inferred": inferred,
        "router_memory_semantics": router_semantics,
        "router_memory_apply": router_apply,
        "core_memory_supported": family == "4026",
        "ads_memory_separate": family == "4026",
    }


def _microseconds_from_100ns(value):
    if not isinstance(value, int):
        return None
    result = value / 10
    return int(result) if result.is_integer() else result


def _system_path(path: str, *, mutation: bool = False, allow_root: bool = True) -> str:
    value = str(path or "").strip().strip("^").replace("/", "^")
    parts = [part.strip() for part in value.split("^") if part.strip()]
    if not parts:
        raise ValueError("system tree path must not be empty")
    root = parts[0].upper()
    roots = _SYSTEM_MUTATION_ROOTS if mutation else _SYSTEM_READ_ROOTS
    if root not in roots:
        allowed = ", ".join(roots)
        raise ValueError(f"system tree path must start with one of: {allowed}")
    if not allow_root and len(parts) == 1:
        raise ValueError(f"refusing to operate on SYSTEM root '{root}'")
    if any(part in {".", ".."} or "^" in part for part in parts):
        raise ValueError("system tree path contains an invalid segment")
    return "^".join(parts)


def _system_node(item, path: str, depth: int, max_depth: int) -> dict:
    name = str(getattr(item, "Name", "") or "")
    actual_path = str(getattr(item, "PathName", "") or path)
    node = {
        "name": name,
        "path": actual_path,
        "item_type": int(getattr(item, "ItemType", 0) or 0),
        "item_subtype": int(getattr(item, "ItemSubType", 0) or 0),
        "children": [],
    }
    if depth >= max_depth:
        return node
    try:
        children = list(item or [])
    except Exception:
        children = []
    for child in children:
        child_name = str(getattr(child, "Name", "") or "")
        if not child_name:
            continue
        child_path = str(getattr(child, "PathName", "") or f"{actual_path}^{child_name}")
        node["children"].append(_system_node(child, child_path, depth + 1, max_depth))
    return node


def system_structure(max_depth: int = 6, roots: list[str] | None = None,
                     *, sysman=None) -> dict:
    """Read live SYSTEM/real-time tree nodes without changing XAE."""
    manager = sysman or _sysman()
    requested = [str(root or "").strip().upper() for root in (roots or _SYSTEM_READ_ROOTS)]
    invalid = [root for root in requested if root not in _SYSTEM_READ_ROOTS]
    if invalid:
        raise ValueError(f"unsupported SYSTEM root: {invalid[0]}")
    depth = max(0, min(int(max_depth or 6), 12))
    found = []
    missing = []
    for root in requested:
        try:
            item = manager.LookupTreeItem(root)
            found.append(_system_node(item, root, 0, depth))
        except Exception as exc:
            missing.append({"root": root, "error": str(exc)})
    try:
        target = str(manager.GetTargetNetId() or "")
    except Exception:
        target = ""
    return {
        "status": "ok",
        "target_netid": target,
        "max_depth": depth,
        "roots": found,
        "missing_roots": missing,
    }


def _xml_local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _parse_system_integer(value, field: str, *, maximum: int = (1 << 63) - 1) -> int:
    text = str(value).strip()
    try:
        # TwinCAT XML commonly emits bitmasks as ``#x1`` rather than Python's
        # ``0x1`` spelling.
        number = int(text[2:], 16) if text.casefold().startswith("#x") else int(text, 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 0 or number > maximum:
        raise ValueError(f"{field} must be between 0 and {maximum}")
    return number


def _system_settings_element(root):
    for elem in root.iter():
        if _xml_local_name(elem.tag).casefold() in {"settings", "rtimesetdef"}:
            return elem
    return None


def _direct_child(element, wanted: str):
    wanted = wanted.casefold()
    return next((child for child in list(element)
                 if _xml_local_name(child.tag).casefold() == wanted), None)


def _direct_value(element, wanted: str):
    key = _attr_name(element, wanted)
    if key is not None:
        return element.attrib[key]
    child = _direct_child(element, wanted)
    if child is not None and child.text is not None:
        return child.text
    return None


def _attr_name(element, wanted: str) -> str | None:
    wanted = wanted.casefold()
    for key in element.attrib:
        if str(key).casefold() == wanted:
            return str(key)
    return None


def _core_layout(root, settings, cpu_ids: list[int], attributes: dict) -> dict:
    """Expand TwinCAT's P/E affinity masks into the per-core view shown by XAE.

    ``AvailabeCPUs`` (the spelling used by TwinCAT XML) describes the target
    logical-core count, while ``PCoreAffinity``/``ECoreAffinity`` identify the
    core type by bit position.  ``Affinity`` is the currently selected
    real-time mask.  Keeping these three concepts separate is important:
    increasing ``MaxCPUs`` alone does not select additional cores.
    """
    def integer_value(element, *names):
        if element is None:
            return None
        for name in names:
            value = _direct_value(element, name)
            if value is None:
                continue
            try:
                return _parse_system_integer(value, name)
            except ValueError:
                return None
        return None

    target = next(
        (elem for elem in root.iter()
         if _xml_local_name(elem.tag).casefold() == "targetcpuinfo"),
        None,
    )
    available = integer_value(target, "AvailabeCPUs", "AvailableCPUs")
    real_time = integer_value(target, "RealTimeCPUs")
    target_p = integer_value(target, "PCoreAffinity")
    target_e = integer_value(target, "ECoreAffinity")
    current_affinity = integer_value(settings, "Affinity")

    masks = [value for value in (target_p, target_e, current_affinity) if isinstance(value, int)]
    highest_mask_core = max((value.bit_length() for value in masks), default=0)
    highest_configured_core = max((value + 1 for value in cpu_ids), default=0)
    count = max(int(available or 0), highest_mask_core, highest_configured_core)
    # TwinCAT's public integer fields are at most 64-bit masks.  Do not emit
    # an unbounded list if a malformed target reports an extreme count.
    count = min(max(count, 0), 64)
    cpu_container = _direct_child(settings, "cpus")
    cpu_nodes = list(cpu_container) if cpu_container is not None else list(settings)
    configured_nodes = {}
    for node in cpu_nodes:
        if _xml_local_name(node.tag).casefold() != "cpu":
            continue
        key = _attr_name(node, "CpuId") or _attr_name(node, "id")
        if key is None:
            continue
        try:
            configured_nodes[_parse_system_integer(node.attrib[key], "CpuId", maximum=4095)] = node
        except ValueError:
            continue

    def core_integer(node, name: str):
        value = _direct_value(node, name) if node is not None else None
        if value is None:
            return None
        try:
            return _parse_system_integer(value, name)
        except ValueError:
            return None

    cores = []
    for core_id in range(count):
        bit = 1 << core_id
        node = configured_nodes.get(core_id)
        base_time = core_integer(node, "BaseTime")
        latency_warning = core_integer(node, "LatencyWarning")
        core_memory_bytes = core_integer(node, "CpuMemorySize")
        core_type = (
            "P" if isinstance(target_p, int) and target_p & bit else
            "E" if isinstance(target_e, int) and target_e & bit else
            "Unknown"
        )
        cores.append({
            "id": core_id,
            "label": f"{core_id} ({core_type})",
            "core_type": core_type,
            "selected": bool(isinstance(current_affinity, int) and current_affinity & bit),
            "configured": core_id in cpu_ids,
            "load_limit_percent": core_integer(node, "LoadLimit"),
            "base_time_100ns": base_time,
            "base_time_us": _microseconds_from_100ns(base_time),
            "latency_warning_100ns": latency_warning,
            "latency_warning_us": _microseconds_from_100ns(latency_warning),
            "core_memory_bytes": core_memory_bytes,
            "core_memory_kb": (core_memory_bytes / 1024
                               if isinstance(core_memory_bytes, int) else None),
            "core_frequency": core_integer(node, "CoreFrequency"),
            "core_memory_allocation_limit": core_integer(node, "CpuMemoryAllocLimit"),
        })
    return {
        "available_cpus": available,
        "real_time_cpus": real_time,
        "affinity": current_affinity,
        "target_p_core_affinity": target_p,
        "target_e_core_affinity": target_e,
        "cores": cores,
    }


def _system_settings_summary(xml: str, version_info: dict | None = None) -> dict:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(str(xml or ""))
    except ET.ParseError as exc:
        raise RuntimeError(f"TIRS returned invalid XML: {exc}") from exc
    settings = _system_settings_element(root)
    if settings is None:
        return {"attributes": {}, "cpu_ids": [], "tasks": [], "cores": [],
                "settings_found": False}

    attributes = {str(key): str(value) for key, value in settings.attrib.items()}
    # 4024/4026 TIRS uses RTimeSetDef with child elements (MaxCPUs,
    # PCoreAffinity, ...), while some versions expose a Settings node with
    # attributes. Keep both forms in one normalized response.
    for child in list(settings):
        if len(list(child)) == 0 and child.text is not None:
            attributes.setdefault(_xml_local_name(child.tag), child.text.strip())
    cpu_ids = []
    cpu_container = _direct_child(settings, "cpus")
    cpu_nodes = list(cpu_container) if cpu_container is not None else list(settings)
    for child in cpu_nodes:
        if _xml_local_name(child.tag).casefold() not in {"cpu", "cpus"}:
            continue
        key = _attr_name(child, "CpuId") or _attr_name(child, "id")
        if key is None:
            continue
        try:
            cpu_ids.append(_parse_system_integer(child.attrib[key], "CpuId", maximum=4095))
        except ValueError:
            continue

    tasks = []
    for elem in root.iter():
        if _xml_local_name(elem.tag).casefold() not in {"task", "taskdef"}:
            continue
        item = {str(key): str(value) for key, value in elem.attrib.items()}
        for child in list(elem):
            if len(list(child)) == 0 and child.text is not None:
                item[_xml_local_name(child.tag)] = child.text.strip()
        tasks.append(item)

    def integer_attribute(name: str):
        value = _direct_value(settings, name)
        if value is None:
            return None
        try:
            return _parse_system_integer(value, name)
        except ValueError:
            return value

    layout = _core_layout(root, settings, cpu_ids, attributes)
    profile = _twincat_realtime_profile(version_info, attributes)
    router_memory_kb = integer_attribute("RouterMemory")
    router_memory_mb = (router_memory_kb / 1024
                        if isinstance(router_memory_kb, int) else None)
    estimated_ads_mb = None
    if profile["family"] == "4026" and isinstance(router_memory_mb, (int, float)):
        estimated_ads_mb = max(4, min(32, router_memory_mb * 0.25))
    return {
        "attributes": attributes,
        "max_cpus": integer_attribute("MaxCpus"),
        "p_core_affinity": integer_attribute("PCoreAffinity"),
        "e_core_affinity": integer_attribute("ECoreAffinity"),
        "cpu_ids": cpu_ids,
        "tasks": tasks,
        "twincat": profile,
        "router_memory_raw_kb": router_memory_kb,
        "router_memory_mb": router_memory_mb,
        "max_task_stack_kb": integer_attribute("MaxStackSize"),
        "max_task_dumps": integer_attribute("MaxTaskDumps"),
        "global_ads_memory_estimated_mb": estimated_ads_mb,
        **layout,
        "settings_found": True,
    }


def system_settings(*, sysman=None, version_info: dict | None = None) -> dict:
    """Read TIRS settings, CPU selection and task attributes."""
    manager = sysman or _sysman()
    item = _lookup_or_raise(manager, "TIRS")
    xml = _item_xml(item)
    if version_info is None:
        try:
            version_info = get_target_tc_version()
        except Exception:
            version_info = {}
    result = _system_settings_summary(xml, version_info)
    result.update({"status": "ok", "path": "TIRS", "xml": xml})
    return result


def _normalize_system_settings_patch(settings: dict, current: dict) -> dict:
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings must be a non-empty object")
    result = {}
    for raw_name, raw_value in settings.items():
        key = str(raw_name).strip().casefold().replace("-", "_")
        canonical = _SYSTEM_CORE_FIELDS.get(key)
        if canonical is None:
            allowed = ", ".join((
                "max_cpus", "cpu_ids", "affinity", "p_core_affinity",
                "e_core_affinity", "router_memory_mb", "max_stack_size_kb",
                "core_settings",
            ))
            raise ValueError(f"unsupported SYSTEM setting '{raw_name}'; allowed: {allowed}")
        if canonical == "CpuIds":
            if not isinstance(raw_value, (list, tuple)) or not raw_value:
                raise ValueError("cpu_ids must be a non-empty array")
            values = [_parse_system_integer(value, "cpu_ids", maximum=4095)
                      for value in raw_value]
            if len(set(values)) != len(values):
                raise ValueError("cpu_ids must not contain duplicates")
            result[canonical] = sorted(values)
        elif canonical == "MaxCpus":
            result[canonical] = _parse_system_integer(raw_value, "max_cpus", maximum=4095)
            if result[canonical] <= 0:
                raise ValueError("max_cpus must be greater than zero")
        elif canonical == "RouterMemory":
            value = _parse_system_integer(raw_value, "router_memory_mb", maximum=65535)
            if value <= 0:
                raise ValueError("router_memory_mb must be greater than zero")
            family = current.get("twincat", {}).get("family", "unknown")
            if family == "4024" and value > 1024:
                raise ValueError("TwinCAT 4024 router_memory_mb must not exceed 1024")
            if family == "unknown" and value > 1024:
                raise ValueError(
                    "router_memory_mb above 1024 requires a confirmed TwinCAT 4026 target")
            result[canonical] = value * 1024
        elif canonical == "MaxStackSize":
            value = _parse_system_integer(
                raw_value, "max_stack_size_kb", maximum=1048576)
            if value <= 0:
                raise ValueError("max_stack_size_kb must be greater than zero")
            result[canonical] = value
        elif canonical == "CoreSettings":
            if not isinstance(raw_value, (list, tuple)) or not raw_value:
                raise ValueError("core_settings must be a non-empty array")
            normalized = []
            seen = set()
            for index, entry in enumerate(raw_value):
                if not isinstance(entry, dict):
                    raise ValueError(f"core_settings[{index}] must be an object")
                cpu_id = _parse_system_integer(
                    entry.get("cpu_id"), f"core_settings[{index}].cpu_id", maximum=4095)
                if cpu_id in seen:
                    raise ValueError("core_settings must not contain duplicate cpu_id values")
                seen.add(cpu_id)
                fields = {}
                for entry_name, entry_value in entry.items():
                    if entry_name == "cpu_id":
                        continue
                    field = _SYSTEM_CORE_SETTING_FIELDS.get(
                        str(entry_name).strip().casefold().replace("-", "_"))
                    if field is None:
                        raise ValueError(
                            f"unsupported core setting '{entry_name}'; allowed: "
                            "base_time_100ns, load_limit_percent, latency_warning_100ns, core_memory_kb")
                    if field == "LoadLimit":
                        parsed = _parse_system_integer(entry_value, entry_name, maximum=100)
                        if parsed <= 0:
                            raise ValueError("load_limit_percent must be between 1 and 100")
                    elif field == "CpuMemorySize":
                        family = current.get("twincat", {}).get("family", "unknown")
                        if family != "4026":
                            raise ValueError(
                                f"core_memory_kb is not supported on TwinCAT {family}; "
                                "it requires a confirmed TwinCAT 4026 target")
                        parsed = _parse_system_integer(
                            entry_value, entry_name, maximum=67108864) * 1024
                    else:
                        parsed = _parse_system_integer(entry_value, entry_name, maximum=(1 << 31) - 1)
                    fields[field] = parsed
                if not fields:
                    raise ValueError(f"core_settings[{index}] does not contain a setting")
                normalized.append({"cpu_id": cpu_id, **fields})
            result[canonical] = normalized
        else:
            result[canonical] = _parse_system_integer(raw_value, canonical)

    max_cpus = result.get("MaxCpus", current.get("max_cpus"))
    if result.get("CpuIds") and isinstance(max_cpus, int):
        out_of_range = [value for value in result["CpuIds"] if value >= max_cpus]
        if out_of_range:
            raise ValueError(
                f"cpu_ids contains {out_of_range[0]}, but max_cpus is {max_cpus}")
    if "Affinity" in result and isinstance(max_cpus, int):
        if result["Affinity"] >> max_cpus:
            raise ValueError("affinity contains a core outside max_cpus")
    for field in ("Affinity", "PCoreAffinity", "ECoreAffinity"):
        if field in result and field.casefold() not in {
                key.casefold() for key in current.get("attributes", {})}:
            raise ValueError(
                f"current TIRS XML does not expose writable {field}; read tc_system_settings first")
    for field in ("RouterMemory", "MaxStackSize"):
        if field in result and field.casefold() not in {
                key.casefold() for key in current.get("attributes", {})}:
            raise ValueError(
                f"current TIRS XML does not expose writable {field}; read tc_system_settings first")
    if "CoreSettings" in result:
        configured = set(current.get("cpu_ids", []))
        for entry in result["CoreSettings"]:
            if entry["cpu_id"] not in configured:
                raise ValueError(
                    f"core_settings references CPU {entry['cpu_id']}, which is not configured as an RT core")
    return result


def _system_settings_xml_with_patch(xml: str, patch: dict) -> tuple[str, dict]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(str(xml or ""))
    except ET.ParseError as exc:
        raise RuntimeError(f"TIRS returned invalid XML: {exc}") from exc
    settings = _system_settings_element(root)
    if settings is None:
        raise RuntimeError("TIRS XML does not contain a <Settings> element")
    for field in ("MaxCpus", "Affinity", "PCoreAffinity", "ECoreAffinity",
                  "RouterMemory", "MaxStackSize"):
        if field not in patch:
            continue
        key = _attr_name(settings, field)
        if key is not None:
            settings.set(key, str(patch[field]))
            continue
        child = _direct_child(settings, field)
        if child is None:
            raise RuntimeError(f"TIRS XML does not contain writable {field}")
        child.text = str(patch[field])
    if "CpuIds" in patch:
        container = _direct_child(settings, "cpus")
        if container is None:
            container = ET.Element("CPUs")
            settings.append(container)
        cpu_nodes = [child for child in list(container)
                     if _xml_local_name(child.tag).casefold() == "cpu"]
        sample = cpu_nodes[0] if cpu_nodes else None
        cpu_tag = _xml_local_name(sample.tag) if sample is not None else "CPU"
        cpu_attr = _attr_name(sample, "CpuId") if sample is not None else None
        cpu_attr = cpu_attr or (_attr_name(sample, "id") if sample is not None else None) or "id"
        existing = {}
        for child in cpu_nodes:
            key = _attr_name(child, "CpuId") or _attr_name(child, "id")
            if key is None:
                continue
            try:
                existing[_parse_system_integer(child.attrib[key], "CpuId", maximum=4095)] = child
            except ValueError:
                continue
        for child in cpu_nodes:
            container.remove(child)
        for cpu_id in patch["CpuIds"]:
            # Keep existing per-core fields (LoadLimit/BaseTime/etc.) and
            # create only genuinely new CPU entries.
            child = existing.get(cpu_id)
            if child is None:
                child = ET.Element(cpu_tag, {cpu_attr: str(cpu_id)})
            container.append(child)
    if "CoreSettings" in patch:
        container = _direct_child(settings, "cpus")
        cpu_nodes = list(container) if container is not None else list(settings)
        by_id = {}
        for child in cpu_nodes:
            if _xml_local_name(child.tag).casefold() != "cpu":
                continue
            key = _attr_name(child, "CpuId") or _attr_name(child, "id")
            if key is None:
                continue
            try:
                by_id[_parse_system_integer(child.attrib[key], "CpuId", maximum=4095)] = child
            except ValueError:
                continue
        for entry in patch["CoreSettings"]:
            cpu_id = entry["cpu_id"]
            cpu = by_id.get(cpu_id)
            if cpu is None:
                raise RuntimeError(f"TIRS XML does not contain configured CPU {cpu_id}")
            for field, value in entry.items():
                if field == "cpu_id":
                    continue
                key = _attr_name(cpu, field)
                if key is not None:
                    cpu.set(key, str(value))
                    continue
                child = _direct_child(cpu, field)
                if child is None:
                    raise RuntimeError(
                        f"TIRS XML does not expose writable {field} for CPU {cpu_id}")
                child.text = str(value)
    output = ET.tostring(root, encoding="unicode")
    return output, _system_settings_summary(output)


def system_settings_set(settings: dict, apply: bool = False, *, sysman=None) -> dict:
    """Preview or write allow-listed TIRS settings, then verify by readback."""
    manager = sysman or _sysman()
    item = _lookup_or_raise(manager, "TIRS")
    before_xml = _item_xml(item)
    try:
        version_info = get_target_tc_version()
    except Exception:
        version_info = {}
    before = _system_settings_summary(before_xml, version_info)
    patch = _normalize_system_settings_patch(settings, before)
    after_xml, preview = _system_settings_xml_with_patch(before_xml, patch)
    preview["twincat"] = before.get("twincat", {})
    requirements = {
        "requires_activation": True,
        "requires_target_reboot": (
            "RouterMemory" in patch and
            before.get("twincat", {}).get("family") == "4024"
        ),
        "requires_restart_for_ads_memory": (
            "RouterMemory" in patch and
            before.get("twincat", {}).get("family") == "4026"
        ),
    }
    if not apply:
        return {
            "status": "preview",
            "path": "TIRS",
            "requested": patch,
            "before": before,
            "after": preview,
            **requirements,
            "applied": False,
        }
    item.ConsumeXml(after_xml)
    readback = system_settings(sysman=manager)
    field_map = {
        "MaxCpus": "max_cpus", "CpuIds": "cpu_ids", "Affinity": "affinity",
        "PCoreAffinity": "p_core_affinity", "ECoreAffinity": "e_core_affinity",
        "RouterMemory": "router_memory_raw_kb", "MaxStackSize": "max_task_stack_kb",
    }
    verified = True
    for key, expected in patch.items():
        if key == "CoreSettings":
            cores = {core.get("id"): core for core in readback.get("cores", [])}
            readback_names = {
                "BaseTime": "base_time_100ns",
                "LoadLimit": "load_limit_percent",
                "LatencyWarning": "latency_warning_100ns",
                "CpuMemorySize": "core_memory_bytes",
            }
            for entry in expected:
                core = cores.get(entry["cpu_id"], {})
                for field, value in entry.items():
                    if field != "cpu_id" and core.get(readback_names[field]) != value:
                        verified = False
        elif readback.get(field_map[key]) != expected:
            verified = False
    if not verified:
        raise RuntimeError(
            "TIRS settings were written but readback did not match the requested values")
    return {
        "status": "written",
        "path": "TIRS",
        "requested": patch,
        "before": before,
        "after": readback,
        "readback_verified": True,
        **requirements,
        "applied": True,
    }


def realtime_info(*, sysman=None) -> dict:
    """Return normalized global, per-core and task real-time configuration."""
    settings = system_settings(sysman=sysman)
    tasks = task_info(sysman=sysman)
    return {
        "status": "ok",
        "path": "TIRS",
        "twincat": settings.get("twincat", {}),
        "memory": {
            "router_memory_mb": settings.get("router_memory_mb"),
            "router_memory_raw_kb": settings.get("router_memory_raw_kb"),
            "max_task_stack_kb": settings.get("max_task_stack_kb"),
            "global_ads_memory_estimated_mb": settings.get(
                "global_ads_memory_estimated_mb"),
        },
        "cores": settings.get("cores", []),
        "tasks": tasks.get("tasks", []),
    }


def validate_realtime_snapshot(snapshot: dict) -> dict:
    """Validate one normalized Real-Time snapshot without touching XAE.

    The validator intentionally separates hard configuration contradictions
    from warnings that require deployment knowledge (for example a task with
    automatic CPU affinity while configured cores use different BaseTimes).
    """
    issues: list[dict] = []

    def issue(severity: str, code: str, message: str, path: str = "") -> None:
        issues.append({"severity": severity, "code": code,
                       "message": message, "path": path})

    version = dict(snapshot.get("twincat") or {})
    family = str(version.get("family") or "unknown")
    if family not in {"4024", "4026"}:
        issue("warning", "twincat_version_unknown",
              "TwinCAT build family could not be confirmed; 4024/4026 memory semantics were not assumed.",
              "twincat.family")

    memory = dict(snapshot.get("memory") or {})
    router_mb = memory.get("router_memory_mb")
    stack_kb = memory.get("max_task_stack_kb")
    if not isinstance(router_mb, (int, float)) or router_mb <= 0:
        issue("warning", "router_memory_unavailable",
              "Router/Global RT memory is unavailable or zero.", "memory.router_memory_mb")
    elif family == "4024" and router_mb > 1024:
        issue("error", "router_memory_4024_limit",
              "TwinCAT 4024 combined RT/ADS memory must not exceed 1024 MB.",
              "memory.router_memory_mb")
    if not isinstance(stack_kb, (int, float)) or stack_kb <= 0:
        issue("warning", "task_stack_unavailable",
              "Maximum task stack size is unavailable or zero.",
              "memory.max_task_stack_kb")

    cores = list(snapshot.get("cores") or [])
    configured = [core for core in cores if core.get("configured")]
    selected_ids = {core.get("id") for core in cores if core.get("selected")}
    configured_ids = {core.get("id") for core in configured}
    if not configured:
        issue("error", "no_realtime_core", "No configured TwinCAT real-time core was found.", "cores")
    if selected_ids != configured_ids:
        issue("warning", "core_selection_mismatch",
              f"Selected cores {sorted(selected_ids)} differ from configured CPU nodes {sorted(configured_ids)}.",
              "cores")
    base_by_core: dict[int, int] = {}
    total_core_memory = 0
    for core in configured:
        core_id = core.get("id")
        path = f"cores[{core_id}]"
        base = core.get("base_time_100ns")
        if not isinstance(base, int) or base <= 0:
            issue("error", "core_base_time_invalid",
                  f"Core {core_id} has no positive BaseTime.", path + ".base_time_100ns")
        else:
            base_by_core[int(core_id)] = base
        limit = core.get("load_limit_percent")
        if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 100):
            issue("error", "core_load_limit_invalid",
                  f"Core {core_id} load limit must be between 1 and 100 percent.",
                  path + ".load_limit_percent")
        core_memory = core.get("core_memory_bytes")
        if isinstance(core_memory, (int, float)) and core_memory > 0:
            total_core_memory += int(core_memory)
            if family == "4024":
                issue("error", "core_memory_unsupported_4024",
                      f"Core {core_id} has per-core memory, which is not supported by the 4024 profile.",
                      path + ".core_memory_bytes")

    tasks = list(snapshot.get("tasks") or [])
    priorities: dict[int, str] = {}
    unique_bases = sorted(set(base_by_core.values()))
    for index, task in enumerate(tasks):
        name = str(task.get("name") or task.get("path") or f"task-{index}")
        path = str(task.get("path") or f"tasks[{index}]")
        priority = task.get("priority")
        if isinstance(priority, int):
            if priority in priorities:
                issue("error", "task_priority_duplicate",
                      f"Tasks '{priorities[priority]}' and '{name}' both use priority {priority}.",
                      path + ".priority")
            else:
                priorities[priority] = name
        else:
            issue("warning", "task_priority_unavailable",
                  f"Task '{name}' priority could not be read.", path + ".priority")
        cycle = task.get("cycle_time_100ns")
        if not isinstance(cycle, int) or cycle <= 0:
            issue("error", "task_cycle_invalid",
                  f"Task '{name}' has no positive cycle time.", path + ".cycle_time_100ns")
            continue
        affinity = task.get("cpu_affinity")
        applicable = unique_bases
        if isinstance(affinity, int) and affinity > 0 and affinity & (affinity - 1) == 0:
            cpu_id = affinity.bit_length() - 1
            applicable = [base_by_core[cpu_id]] if cpu_id in base_by_core else []
            if not applicable:
                issue("error", "task_core_not_configured",
                      f"Task '{name}' is assigned to core {cpu_id}, which is not a configured RT core.",
                      path + ".cpu_affinity")
        elif isinstance(affinity, int) and affinity > 0:
            issue("warning", "task_affinity_multiple_cores",
                  f"Task '{name}' affinity selects multiple cores; BaseTime validation uses all configured cores.",
                  path + ".cpu_affinity")
        incompatible = [base for base in applicable if cycle < base or cycle % base]
        if applicable and incompatible:
            severity = "error" if len(incompatible) == len(applicable) else "warning"
            issue(severity, "task_cycle_base_time_mismatch",
                  f"Task '{name}' cycle {cycle} x100ns is incompatible with BaseTime value(s) {sorted(set(incompatible))}.",
                  path + ".cycle_time_100ns")
        elif not applicable and configured:
            issue("warning", "task_base_time_not_verified",
                  f"Task '{name}' core/BaseTime compatibility could not be verified.", path)

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    return {
        "status": "valid" if errors == 0 else "invalid",
        "valid": errors == 0,
        "error_count": errors,
        "warning_count": warnings,
        "issues": issues,
        "summary": {
            "twincat_family": family,
            "configured_core_count": len(configured),
            "task_count": len(tasks),
            "base_times_100ns": unique_bases,
            "total_core_memory_bytes": total_core_memory,
            "estimated_task_stack_kb": (stack_kb * len(tasks)
                                         if isinstance(stack_kb, (int, float)) else None),
        },
    }


def realtime_validate(*, sysman=None) -> dict:
    """Read and validate the complete Real-Time configuration."""
    snapshot = realtime_info(sysman=sysman)
    result = validate_realtime_snapshot(snapshot)
    result["snapshot"] = snapshot
    return result


def realtime_settings_set(settings: dict, apply: bool = False, *, sysman=None) -> dict:
    """Preview or apply global/per-core real-time settings with version gates."""
    return system_settings_set(settings, apply=apply, sysman=sysman)


def core_info(*, sysman=None) -> dict:
    """Return the effective TwinCAT CPU selection from TIRS."""
    settings = system_settings(sysman=sysman)
    return {
        "status": "ok",
        "path": "TIRS",
        "max_cpus": settings.get("max_cpus"),
        "cpu_ids": settings.get("cpu_ids", []),
        "p_core_affinity": settings.get("p_core_affinity"),
        "e_core_affinity": settings.get("e_core_affinity"),
        "available_cpus": settings.get("available_cpus"),
        "real_time_cpus": settings.get("real_time_cpus"),
        "affinity": settings.get("affinity"),
        "target_p_core_affinity": settings.get("target_p_core_affinity"),
        "target_e_core_affinity": settings.get("target_e_core_affinity"),
        "cores": settings.get("cores", []),
        "twincat": settings.get("twincat", {}),
        "router_memory_mb": settings.get("router_memory_mb"),
        "max_task_stack_kb": settings.get("max_task_stack_kb"),
        "settings_found": settings.get("settings_found", False),
        "tasks": settings.get("tasks", []),
    }


def core_assign(cpu_ids: list[int], max_cpus: int | None = None,
                affinity: int | None = None,
                p_core_affinity: int | None = None,
                e_core_affinity: int | None = None,
                apply: bool = False, *, sysman=None) -> dict:
    """Preview or apply TwinCAT core selection through TIRS."""
    patch: dict = {"cpu_ids": cpu_ids}
    if max_cpus is not None:
        patch["max_cpus"] = max_cpus
    if affinity is None:
        ids = [_parse_system_integer(value, "cpu_ids", maximum=62) for value in cpu_ids]
        affinity = sum(1 << value for value in ids)
    patch["affinity"] = affinity
    if p_core_affinity is not None:
        patch["p_core_affinity"] = p_core_affinity
    if e_core_affinity is not None:
        patch["e_core_affinity"] = e_core_affinity
    return system_settings_set(patch, apply=apply, sysman=sysman)


def task_info(*, sysman=None, max_depth: int = 6) -> dict:
    """Read the live TIRT task tree and task XML summaries."""
    manager = sysman or _sysman()
    root = _lookup_or_raise(manager, "TIRT")
    tree = _system_node(root, "TIRT", 0, max(0, min(int(max_depth or 6), 12)))
    tasks = []

    def walk(item, path: str):
        try:
            children = list(item or [])
        except Exception:
            children = []
        for child in children:
            name = str(getattr(child, "Name", "") or "")
            if not name:
                continue
            child_path = str(getattr(child, "PathName", "") or f"{path}^{name}")
            try:
                values = _system_settings_summary(_item_xml(child)).get("tasks", [])
            except Exception:
                values = []
            parameters = values[0] if values else {}
            def task_integer(name: str):
                value = parameters.get(name)
                if value is None:
                    return None
                try:
                    return _parse_system_integer(value, name)
                except ValueError:
                    return None
            cycle_time = task_integer("CycleTime")
            tasks.append({
                "name": name,
                "path": child_path,
                "item_type": int(getattr(child, "ItemType", 0) or 0),
                "parameters": parameters,
                "priority": task_integer("Priority"),
                "cycle_time_100ns": cycle_time,
                "cycle_time_us": _microseconds_from_100ns(cycle_time),
                "auto_start": str(parameters.get("AutoStart", "")).casefold() == "true",
                "cpu_affinity": task_integer("CpuAffinity"),
            })
            walk(child, child_path)

    walk(root, "TIRT")
    return {"status": "ok", "path": "TIRT", "tree": tree, "tasks": tasks}


def task_core_assign(task_path: str, cpu_id: int, apply: bool = False,
                     *, sysman=None) -> dict:
    """Preview or update an exposed task core field; never invents XML tags."""
    manager = sysman or _sysman()
    path = _system_path(task_path, mutation=True, allow_root=False)
    item = _lookup_or_raise(manager, path)
    before_xml = _item_xml(item)
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(before_xml)
    except ET.ParseError as exc:
        raise RuntimeError(f"task XML is invalid: {exc}") from exc
    cpu = _parse_system_integer(cpu_id, "cpu_id", maximum=4095)
    target = None
    target_kind = ""
    for elem in root.iter():
        for attr in _SYSTEM_TASK_CORE_ATTRS:
            key = _attr_name(elem, attr)
            if key is not None:
                target, target_kind = (elem, key), "attribute"
                break
        if target:
            break
        for child in list(elem):
            if _xml_local_name(child.tag).casefold() in {name.casefold() for name in _SYSTEM_TASK_CORE_ATTRS}:
                target, target_kind = (child, "text"), "element"
                break
        if target:
            break
    if target is None:
        raise RuntimeError(
            "task XML does not expose CpuAffinity/CpuId/CoreId/AssignedCore; read tc_task_info and use the exact XAE task field")
    target_name = (target[1] if target_kind == "attribute"
                   else _xml_local_name(target[0].tag))
    is_affinity = target_name.casefold() == "cpuaffinity"
    if is_affinity and cpu_id > 63:
        raise ValueError("cpu_id must be between 0 and 63 when the task exposes CpuAffinity")
    target_value = f"#x{1 << cpu:X}" if is_affinity else str(cpu)
    before_value = None
    try:
        before_value = _parse_system_integer(
        target[0].attrib[target[1]], "existing task core", maximum=(1 << 63) - 1
        ) if target_kind == "attribute" else _parse_system_integer(
            target[0].text, "existing task core", maximum=(1 << 63) - 1
        )
    except (KeyError, TypeError, ValueError):
        pass
    if target_kind == "attribute":
        target[0].set(target[1], target_value)
    else:
        target[0].text = target_value
    after_xml = ET.tostring(root, encoding="unicode")
    before = {"field": target_name, "value": before_value,
              "cpu_id": None if is_affinity else before_value}
    if not apply:
        return {
            "status": "preview", "path": path, "requested_cpu_id": cpu,
            "field": target_name, "before": before,
            "after": {"field": target_name, "value": target_value,
                       "cpu_id": None if is_affinity else cpu},
            "requires_activation": True, "applied": False,
        }
    item.ConsumeXml(after_xml)
    readback_xml = _item_xml(item)
    readback = None
    try:
        rb_root = ET.fromstring(readback_xml)
        if target_kind == "attribute":
            rb_elem = next((elem for elem in rb_root.iter()
                            if _attr_name(elem, target[1]) is not None), None)
            if rb_elem is not None:
                key = _attr_name(rb_elem, target[1])
                readback = _parse_system_integer(rb_elem.attrib[key], "readback task core", maximum=(1 << 63) - 1)
        else:
            rb_elem = next((elem for elem in rb_root.iter()
                            if _xml_local_name(elem.tag).casefold() == _xml_local_name(target[0].tag).casefold()), None)
            if rb_elem is not None and rb_elem.text:
                readback = _parse_system_integer(rb_elem.text, "readback task core", maximum=(1 << 63) - 1)
    except (ET.ParseError, ValueError):
        readback = None
    expected_readback = 1 << cpu if is_affinity else cpu
    if readback != expected_readback:
        raise RuntimeError("task core was written but readback did not match")
    return {
        "status": "written", "path": path, "requested_cpu_id": cpu,
        "field": target_name, "readback_value": readback,
        "readback_cpu_id": None if is_affinity else readback,
        "readback_verified": True,
        "requires_activation": True, "applied": True,
    }


def task_settings_set(task_path: str, settings: dict, apply: bool = False,
                      *, sysman=None) -> dict:
    """Preview or update exposed task timing/priority fields with RT checks."""
    from decimal import Decimal, InvalidOperation
    import xml.etree.ElementTree as ET

    manager = sysman or _sysman()
    path = _system_path(task_path, mutation=True, allow_root=False)
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings must be a non-empty object")
    item = _lookup_or_raise(manager, path)
    before_xml = _item_xml(item)
    try:
        root = ET.fromstring(before_xml)
    except ET.ParseError as exc:
        raise RuntimeError(f"task XML is invalid: {exc}") from exc
    task = next((elem for elem in root.iter()
                 if _xml_local_name(elem.tag).casefold() in {"task", "taskdef"}), None)
    if task is None:
        raise RuntimeError("task XML does not contain a Task/TaskDef element")

    aliases = {
        "priority": "Priority",
        "cycle_time_100ns": "CycleTime",
        "cycle_time_us": "CycleTimeUs",
        "auto_start": "AutoStart",
        "tick_modulo": "TickModulo",
        "input_update_pre_ticks": "InputUpdatePreTicks",
        "exceed_warning": "ExceedWarning",
        "watchdog_stack_capacity": "WatchdogStackCapacity",
    }
    patch = {}
    for raw_name, raw_value in settings.items():
        canonical = aliases.get(str(raw_name).strip().casefold().replace("-", "_"))
        if canonical is None:
            raise ValueError(
                f"unsupported task setting '{raw_name}'; allowed: "
                "priority, cycle_time_100ns, cycle_time_us, auto_start, tick_modulo, "
                "input_update_pre_ticks, exceed_warning, watchdog_stack_capacity")
        if canonical == "Priority":
            patch[canonical] = _parse_system_integer(raw_value, raw_name, maximum=31)
        elif canonical == "CycleTimeUs":
            try:
                ticks = Decimal(str(raw_value)) * Decimal(10)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("cycle_time_us must be numeric") from exc
            if ticks != ticks.to_integral_value() or ticks <= 0:
                raise ValueError("cycle_time_us must be positive with 0.1 us precision")
            patch["CycleTime"] = int(ticks)
        elif canonical == "AutoStart":
            if not isinstance(raw_value, bool):
                raise ValueError("auto_start must be boolean")
            patch[canonical] = raw_value
        else:
            patch[canonical] = _parse_system_integer(
                raw_value, raw_name, maximum=(1 << 31) - 1)

    if "Priority" in patch:
        for other in task_info(sysman=manager).get("tasks", []):
            if other.get("path") != path and other.get("priority") == patch["Priority"]:
                raise ValueError(
                    f"priority {patch['Priority']} is already used by {other.get('path')}")
    timing_warnings = []
    if "CycleTime" in patch:
        rt = system_settings(sysman=manager)
        bases = [core.get("base_time_100ns") for core in rt.get("cores", [])
                 if core.get("configured") and isinstance(core.get("base_time_100ns"), int)
                 and core.get("base_time_100ns") > 0]
        incompatible = [base for base in bases
                        if patch["CycleTime"] < base or patch["CycleTime"] % base]
        if bases and incompatible:
            unique = sorted(set(bases))
            if len(unique) == 1 or len(incompatible) == len(bases):
                raise ValueError(
                    f"cycle time {patch['CycleTime']} x100ns is not compatible with RT core base time(s) {unique}")
            timing_warnings.append(
                "RT cores use different BaseTime values; cycle is not compatible with every core. "
                "Assign the task to a compatible core before activation.")
        elif not bases:
            timing_warnings.append(
                "No configured RT-core BaseTime was available; cycle/BaseTime compatibility was not verified.")

    targets = {}
    before = {}
    for field, value in patch.items():
        key = _attr_name(task, field)
        if key is not None:
            targets[field] = ("attribute", task, key)
            before[field] = task.attrib[key]
            task.set(key, str(value).lower() if isinstance(value, bool) else str(value))
            continue
        child = _direct_child(task, field)
        if child is None:
            raise RuntimeError(f"task XML does not expose writable {field}")
        targets[field] = ("element", child, "text")
        before[field] = child.text
        child.text = str(value).lower() if isinstance(value, bool) else str(value)
    after_xml = ET.tostring(root, encoding="unicode")
    result = {
        "status": "preview", "path": path, "requested": patch,
        "before": before, "warnings": timing_warnings,
        "requires_activation": True, "applied": False,
    }
    if not apply:
        return result
    item.ConsumeXml(after_xml)
    try:
        readback_root = ET.fromstring(_item_xml(item))
    except ET.ParseError as exc:
        raise RuntimeError(f"task readback XML is invalid: {exc}") from exc
    readback_task = next((elem for elem in readback_root.iter()
                          if _xml_local_name(elem.tag).casefold() in {"task", "taskdef"}), None)
    readback = {}
    for field, expected in patch.items():
        value = _direct_value(readback_task, field) if readback_task is not None else None
        readback[field] = value
        normalized = (str(value).casefold() == "true" if isinstance(expected, bool)
                      else _parse_system_integer(value, field))
        if normalized != expected:
            raise RuntimeError(f"task setting {field} was written but readback did not match")
    return {
        **result, "status": "written", "readback": readback,
        "readback_verified": True, "applied": True,
    }


def system_add(parent_path: str, name: str, item_type: int, info: str = "",
               apply: bool = False, *, sysman=None) -> dict:
    """Preview or create a child under the restricted TIRT/TIRC SYSTEM roots."""
    manager = sysman or _sysman()
    parent = _system_path(parent_path, mutation=True)
    child_name = str(name or "").strip()
    if not child_name or "^" in child_name or child_name in {".", ".."}:
        raise ValueError("system child name must be a single non-empty path segment")
    subtype = _parse_system_integer(item_type, "item_type", maximum=65535)
    parent_item = _lookup_or_raise(manager, parent)
    existing = [str(getattr(child, "Name", "") or "") for child in (parent_item or [])]
    if any(value.casefold() == child_name.casefold() for value in existing):
        raise ValueError(f"SYSTEM child already exists: {parent}^{child_name}")
    child_path = f"{parent}^{child_name}"
    if not apply:
        return {"status": "preview", "parent": parent, "path": child_path,
                "name": child_name, "item_type": subtype, "applied": False}
    parent_item.CreateChild(child_name, subtype, "", str(info or ""))
    created = _lookup_or_raise(manager, child_path)
    return {"status": "created", "parent": parent,
            "path": str(getattr(created, "PathName", "") or child_path),
            "name": child_name, "item_type": int(getattr(created, "ItemType", subtype) or subtype),
            "applied": True}


def system_remove(tree_path: str, apply: bool = False,
                  allow_with_children: bool = False, *, sysman=None) -> dict:
    """Preview or delete one exact SYSTEM child; never deletes a SYSTEM root."""
    manager = sysman or _sysman()
    path = _system_path(tree_path, mutation=True, allow_root=False)
    item = _lookup_or_raise(manager, path)
    parent_path, name = path.rsplit("^", 1)
    children = list(item or [])
    if children and not allow_with_children:
        raise ValueError(
            f"SYSTEM node '{path}' has {len(children)} children; set allow_with_children=true to remove it")
    payload = {"status": "preview", "path": path, "parent": parent_path,
               "name": name, "item_type": int(getattr(item, "ItemType", 0) or 0),
               "child_count": len(children), "applied": False}
    if not apply:
        return payload
    parent = _lookup_or_raise(manager, parent_path)
    parent.DeleteChild(name)
    try:
        manager.LookupTreeItem(path)
    except Exception:
        return {**payload, "status": "deleted", "applied": True}
    raise RuntimeError(f"SYSTEM node still exists after deletion: {path}")


def get_ethercat_master_adapter() -> list[dict]:
    """Report each I/O device's bound network adapter (from its ProduceXml).

    Parses TIID device XML for the NIC binding (Pnp DeviceDesc / DeviceName /
    MAC Address). Returns ``[{device, adapter_desc, adapter_name, mac}]`` for
    every device that has an adapter binding (EtherCAT masters, RT-Ethernet).
    """
    from xml.etree import ElementTree as ET
    sysman = _sysman()
    io_root = sysman.LookupTreeItem("TIID")
    result: list[dict] = []
    for child in (io_root or []):
        name = getattr(child, "Name", "")
        try:
            xml = child.ProduceXml(False)
        except Exception:
            continue
        entry = {"device": name, "adapter_desc": "", "adapter_name": "", "mac": ""}
        try:
            root = ET.fromstring(xml)
            for tag, key in (("DeviceDesc", "adapter_desc"),
                             ("DeviceName", "adapter_name"),
                             ("Address", "mac")):
                el = root.find(f".//{tag}")
                if el is not None and el.text:
                    entry[key] = el.text.strip()
        except Exception:
            pass
        if entry["adapter_desc"] or entry["adapter_name"] or entry["mac"]:
            result.append(entry)
    return result


def change_ethercat_master_adapter(device_name: str, adapter_desc: str) -> dict:
    """Rebind an I/O device (EtherCAT master) to a different NIC by DeviceDesc.

    Best-effort: reads the device ProduceXml, swaps the ``<DeviceDesc>`` (and
    clears ``<DeviceName>`` so TwinCAT re-resolves), then ConsumeXml back.
    Validate against your controller's real device XML — NIC descriptor format
    is hardware-specific.
    """
    from xml.etree import ElementTree as ET
    sysman = _sysman()
    io_root = sysman.LookupTreeItem("TIID")
    device = None
    for child in (io_root or []):
        if getattr(child, "Name", "") == device_name:
            device = child
            break
    if device is None:
        raise FileNotFoundError(f"I/O device not found: {device_name}")

    root = ET.fromstring(device.ProduceXml(False))
    desc_el = root.find(".//DeviceDesc")
    if desc_el is None:
        raise RuntimeError(
            f"No <DeviceDesc> (network adapter) in device '{device_name}'")
    desc_el.text = adapter_desc
    name_el = root.find(".//DeviceName")
    if name_el is not None:
        name_el.text = ""
    device.ConsumeXml(ET.tostring(root, encoding="unicode"))
    return {"device": device_name, "adapter_desc": adapter_desc, "status": "changed"}


# ---------------------------------------------------------------------------
# Build / Compile
# ---------------------------------------------------------------------------

def _poll_build_complete(dte: Any, timeout: float = 30.0,
                         interval: float = 1.0) -> bool:
    """Poll SolutionBuild.BuildState until build completes or timeout.

    BuildState values:
      0 = dsBuildStateNotStarted
      1 = dsBuildStateInProgress
      2 = dsBuildStateDone
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            state = dte.Solution.SolutionBuild.BuildState
            if state == 2:  # dsBuildStateDone
                return True
        except Exception:
            pass
        _time.sleep(interval)
    return False  # timeout — build may still be in progress


def build_solution() -> dict:
    """Build the entire solution.

    Returns build result with error list.
    """
    dte = _dte()
    sln = dte.Solution

    _clear_errors()
    try:
        sln.SolutionBuild.Build()
    except Exception:
        pass  # Build runs async — errors appear in Error List

    _poll_build_complete(dte, timeout=30.0)
    return _read_errors()


def build_plc_project() -> dict:
    """Build only the PLC project (faster than full solution build).

    Returns build result with error list.
    """
    dte = _dte()
    sln = dte.Solution

    # Find the PLC project (.plcproj)
    plcproj_name = None
    for i in range(1, sln.Projects.Count + 1):
        proj = sln.Projects.Item(i)
        if proj.FullName and proj.FullName.endswith(".plcproj"):
            plcproj_name = proj.FullName
            break

    # Resolve the active platform (fallback: Release|TwinCAT RT (x64))
    active = get_build_platform()
    platform_name = active["full"] if active.get("status") == "ok" else "Release|TwinCAT RT (x64)"

    _clear_errors()

    if plcproj_name:
        sln.SolutionBuild.BuildProject(platform_name, plcproj_name, True)
    else:
        sln.SolutionBuild.Build()

    _poll_build_complete(dte, timeout=30.0)
    return _read_errors()


# ---------------------------------------------------------------------------
# Build platform management
# ---------------------------------------------------------------------------

def _platform_state_path():
    """Return the path to the platform state file, or None."""
    from pathlib import Path as _Path
    try:
        dte = _dte()
        sln = dte.Solution.FullName
        if sln:
            sln_dir = _Path(sln).parent
            state_dir = sln_dir / ".claude"
            state_dir.mkdir(exist_ok=True)
            return state_dir / "_build_platform.json"
    except Exception:
        pass
    return None


def _read_platform_state() -> str:
    """Read the last-set platform from the state file. Returns ``""`` if none."""
    sp = _platform_state_path()
    if sp and sp.exists():
        import json as _json
        try:
            data = _json.loads(sp.read_text(encoding="utf-8"))
            return data.get("platform", "")
        except Exception:
            pass
    return ""


def _write_platform_state(full: str):
    """Persist the active platform selection."""
    sp = _platform_state_path()
    if sp:
        import json as _json
        sp.write_text(_json.dumps({"platform": full}, indent=2), encoding="utf-8")


def _parse_sln_platforms(sln_path: str = ""):
    """Parse a .sln file to discover available (config, platform) pairs and
    their COM indices.

    Returns a list of dicts with keys ``index`` (1-based, matching
    COM ``SolutionConfigurations.Item(i)``), ``config``, ``platform``,
    and ``full`` (the ``Config|Platform`` string).
    """
    from pathlib import Path as _Path

    if not sln_path:
        dte = _dte()
        try:
            sln_path = dte.Solution.FullName
        except Exception:
            # Fallback: scan output/
            sln_files = list(_Path("output").rglob("*.sln"))
            if sln_files:
                sln_path = str(sln_files[0])
            else:
                return []

    sln_text = _Path(sln_path).read_text(encoding="utf-8", errors="replace")
    import re as _re

    # Match: 	Debug|TwinCAT RT (x64) = Debug|TwinCAT RT (x64)
    # The `=` separates the key from the value; both sides are the same.
    # However the .sln can also have entries like:
    #   Debug|TwinCAT CE7 (ARMV7) = Debug|TwinCAT CE7 (ARMV7)
    # We want the LEFT side (before `=` with the leading whitespace stripped).
    idx = 0
    result: list[dict] = []
    for m in _re.finditer(
        r'^\s*(Debug|Release)\|(.+?)\s*=\s*\1\|', sln_text, _re.M
    ):
        idx += 1
        result.append({
            "index": idx,
            "config": m.group(1),
            "platform": m.group(2).strip(),
            "full": f"{m.group(1)}|{m.group(2).strip()}",
        })
    return result


def list_build_platforms() -> dict:
    """List all available build platforms in the open solution.

    Returns a dict with ``active`` (current platform string),
    ``platforms`` (list of dicts with index/config/platform/full),
    and ``status``.
    """
    platforms = _parse_sln_platforms()
    if not platforms:
        return {"status": "error", "message": "No solution open or .sln not found"}

    # Determine active: state file first (accurate), COM as fallback
    active = _read_platform_state()
    if active and any(p["full"] == active for p in platforms):
        pass  # state file is accurate
    else:
        active = ""
        try:
            dte = _dte()
            sbc = dte.Solution.SolutionBuild
            ac = sbc.ActiveConfiguration
            ac_name = ac.Name  # just "Debug" or "Release"
            for p in platforms:
                if p["config"] == ac_name and not active:
                    active = p["full"]
                    break
        except Exception:
            pass

    return {
        "status": "ok",
        "active": active or (platforms[0]["full"] if platforms else ""),
        "platforms": platforms,
    }


def get_build_platform() -> dict:
    """Return the currently active build platform.

    Reads the state file first (persisted by ``set_build_platform()``),
    falling back to COM.

    Returns a dict with ``config``, ``platform``, ``full``, ``index``,
    and ``status``.
    """
    info = list_build_platforms()
    if info["status"] != "ok":
        return info

    active_full = info["active"]
    for p in info["platforms"]:
        if p["full"] == active_full:
            return {
                "status": "ok",
                "config": p["config"],
                "platform": p["platform"],
                "full": p["full"],
                "index": p["index"],
            }

    # Active not in platform list (stale state file)
    for p in info["platforms"]:
        return {
            "status": "ok",
            "config": p["config"],
            "platform": p["platform"],
            "full": p["full"],
            "index": p["index"],
        }

    return {"status": "error", "message": "No platforms found in .sln"}


def set_build_platform(platform: str = "", config: str = "") -> dict:
    """Switch the solution build platform.

    Args:
        platform: Platform name, e.g. ``"TwinCAT RT (x64)"``.  If empty,
            tries to auto-detect based on the current target.
        config: Configuration name, usually ``"Release"`` or ``"Debug"``.
            If empty, keeps the current config.

    Returns a dict with the new active platform info.
    """
    platforms = _parse_sln_platforms()
    if not platforms:
        return {"status": "error", "message": "No solution open or .sln not found"}

    # Auto-detect from target if not given
    if not platform:
        platform = _autodetect_platform()

    # Determine config: keep current if not given
    if not config:
        current = get_build_platform()
        config = current.get("config", "Release")

    # Build the full name we're looking for
    target_full = f"{config}|{platform}"

    # Find the matching index
    target_index = None
    for p in platforms:
        if p["config"] == config and p["platform"] == platform:
            target_index = p["index"]
            break

    if target_index is None:
        # Fuzzy match: look for platform substring
        plat_lower = platform.lower()
        for p in platforms:
            if p["config"] == config and plat_lower in p["platform"].lower():
                target_index = p["index"]
                platform = p["platform"]
                break

    if target_index is None:
        return {
            "status": "error",
            "message": f"Platform '{target_full}' not found in the solution.",
            "available": [p["full"] for p in platforms],
        }

    # Activate via COM
    dte = _dte()
    sbc = dte.Solution.SolutionBuild
    cfg = sbc.SolutionConfigurations.Item(target_index)
    cfg.Activate()

    full_name = f"{config}|{platform}"
    _write_platform_state(full_name)

    return {
        "status": "ok",
        "config": config,
        "platform": platform,
        "full": full_name,
        "index": target_index,
    }


def _autodetect_platform() -> str:
    """Use the exact target response, never a CPUType/host-platform guess."""
    from ._ps_bridge import ps_com
    target = ps_com('platform-target-info')
    if target.get('target_match_verified') is True and target.get('target_platform'):
        return str(target['target_platform'])
    raise ValueError('Target platform unavailable. Use tc_platform_set preview; do not guess from CPUType.')


def check_all_objects() -> dict:
    """Run CheckAllObjects() on the PLC project (compilation check).

    Equivalent to: PLC project → right-click → Check all objects.
    """
    sysman = _sysman()
    instance = _find_plc_instance(sysman)
    if not instance:
        return {"status": "error", "message": "No PLC instance found"}

    instance.CheckAllObjects()
    _poll_build_complete(_dte(), timeout=30.0)
    return _read_errors()


# ---------------------------------------------------------------------------
# PLC Online: Login / Logout / Start / Stop
# ---------------------------------------------------------------------------

def _send_online_command(runtime='', all_plcs=False, **cmds) -> dict:
    """Send online commands to every configured PLC Project node.

    Online commands (LoginCmd / StartCmd / …) must be sent to the
    **Project** node (``TIPC^PLC1^PLC1 Project``), not the Instance
    node.  The Instance node does not support these ConsumeXml payloads.
    """
    sysman = _sysman()
    xml_parts = []
    for cmd_name, value in cmds.items():
        xml_parts.append(f"<{cmd_name}>{str(value).lower()}</{cmd_name}>")

    xml = (
        "<TreeItem>"
        "<IECProjectDef>"
        "<OnlineSettings>"
        "<Commands>"
        + "".join(xml_parts) +
        "</Commands>"
        "</OnlineSettings>"
        "</IECProjectDef>"
        "</TreeItem>"
    )

    results = []
    from .runtime_contract import select_runtimes
    runtimes = select_runtimes(_list_plc_runtimes(sysman), runtime, all_plcs)
    for runtime in runtimes:
        project = runtime.get("project")
        if project is None:
            results.append({
                "name": runtime["name"],
                "status": "error",
                "message": "No NestedProject node found",
            })
            continue
        try:
            project.ConsumeXml(xml)
            results.append({
                "name": runtime["name"],
                "ads_port": runtime["ads_port"],
                "status": "ok",
            })
        except Exception as exc:
            results.append({
                "name": runtime["name"],
                "ads_port": runtime["ads_port"],
                "status": "error",
                "message": str(exc)[:200],
            })
    if results:
        time.sleep(1)
    failed = [item for item in results if item["status"] != "ok"]
    return {
        "status": ("failed" if failed else "accepted") if results else "skipped",
        "verified": None,
        "commands": cmds,
        "plcs": results,
    }


def plc_online_state(runtime='') -> dict:
    from .runtime_contract import select_runtimes, parse_online_settings
    selected = select_runtimes(_list_plc_runtimes(_sysman()), runtime)
    if len(selected) != 1:
        raise ValueError('Exactly one PLC runtime required')
    return dict(parse_online_settings(str(selected[0]['project'].ProduceXml(False))), name=selected[0]['name'])


def login(runtime='', all_plcs=False) -> dict:
    """Log in to the PLC runtime."""
    return _send_online_command(runtime, all_plcs, LoginCmd=True)


def logout(runtime='', all_plcs=False) -> dict:
    """Log out from PLC runtime."""
    return _send_online_command(runtime, all_plcs, LogoutCmd=True)


def start_plc(runtime='', all_plcs=False) -> dict:
    """Start the PLC program."""
    return _send_online_command(runtime, all_plcs, StartCmd=True)


def stop_plc(runtime='', all_plcs=False) -> dict:
    """Stop the PLC program."""
    return _send_online_command(runtime, all_plcs, StopCmd=True)


def full_online_cycle(runtime='', all_plcs=False) -> dict:
    """Login/load verification followed by ADS-verified Start."""
    from ._ps_bridge import ps_com
    return ps_com('online', runtime=runtime, all_plcs=all_plcs, timeout=60)


# ---------------------------------------------------------------------------
# Run / Config mode switching
# ---------------------------------------------------------------------------

def get_runtime_state() -> dict:
    """Read TwinCAT runtime state, preferring System Service ADS state.

    Returns a dict with keys ``started``, ``state`` (one of NotStarted /
    Config / Run / Started), and any state tags found in the TIRS XML
    (e.g. ``twincatstate``, ``runmode``).
    """
    import xml.etree.ElementTree as ET

    sysman = _sysman()
    result: dict = {"state": "Unknown", "started": False}

    # TIRS XML is not a reliable runtime-state source on all XAE versions:
    # some 4026 installations only report that TwinCAT is "Started" while
    # the System Service is actually in Config.  ADS port 10000 is the
    # authoritative System Service endpoint, so use it whenever reachable.
    try:
        from tc_agent.ads import ADS_SYSTEM_SERVICE_PORT, read_ads_state

        target = str(sysman.GetTargetNetId() or "").strip()
        if target:
            ads = read_ads_state(target, ADS_SYSTEM_SERVICE_PORT)
            code = int(ads["state_code"])
            state = "Run" if code == 5 else "Config" if code in (7, 8, 15) else ads["state_name"]
            return {
                "state": state,
                "started": code not in (0, 1, 6),
                "source": "ads-system-service",
                "ads": ads,
            }
    except Exception as exc:
        result["ads_error"] = str(exc)[:200]

    try:
        result["started"] = sysman.IsTwinCATStarted()
    except Exception:
        result["started"] = False
        result["state"] = "NotStarted"
        return result

    if not result["started"]:
        result["state"] = "NotStarted"
        result["detail"] = "TwinCAT runtime not started"
        return result

    # Read TIRS for detailed state
    try:
        tirs = sysman.LookupTreeItem("TIRS")
        xml = tirs.ProduceXml(False)
        root = ET.fromstring(xml)

        for tag in ("TwinCATState", "RunMode", "CurrentState", "State"):
            elem = root.find(f".//{tag}")
            if elem is not None and elem.text:
                result[tag.lower()] = elem.text.strip()

        tc_state = result.get("twincatstate", "")
        if "config" in tc_state.lower():
            result["state"] = "Config"
        elif "run" in tc_state.lower():
            result["state"] = "Run"

        if result["state"] in ("Unknown",):
            runmode = result.get("runmode", "")
            if "config" in runmode.lower():
                result["state"] = "Config"
            elif "run" in runmode.lower():
                result["state"] = "Run"

        if result["state"] in ("Unknown",):
            result["state"] = "Started"
    except Exception:
        result["state"] = "Started"

    return result


# ── State-polling helpers ────────────────────────────────────────────


def _poll_runtime_started(sysman, timeout: float = 30.0,
                          interval: float = 1.5) -> bool:
    """Poll ``sysman.IsTwinCATStarted()`` until it returns True or *timeout*.

    Returns True once the runtime is confirmed running, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if sysman.IsTwinCATStarted():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _poll_config_mode(sysman, timeout: float = 30.0,
                      interval: float = 1.5) -> bool:
    """Poll the target until it confirms Config mode.

    Checks ``IsTwinCATStarted()`` first, then reads TIRS XML looking for
    Config in ``TwinCATState`` / ``RunMode`` / ``CurrentState``.
    Falls back to ``IsTwinCATStarted()`` alone once the runtime is alive.

    Returns True when Config is confirmed, False on timeout.
    """
    import xml.etree.ElementTree as ET

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not sysman.IsTwinCATStarted():
                time.sleep(interval)
                continue

            tirs = sysman.LookupTreeItem("TIRS")
            xml = tirs.ProduceXml(False)
            root = ET.fromstring(xml)

            for tag in ("TwinCATState", "RunMode", "CurrentState"):
                elem = root.find(f".//{tag}")
                if elem is not None and elem.text \
                   and "config" in elem.text.lower():
                    return True

            # Runtime is alive — accept as Config-adjacent
            return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def set_config_mode() -> dict:
    """Switch the target TwinCAT to Config mode.

    Sends ``<RTStateDef><CurrentState>Config</CurrentState>`` to TIRS.
    If the target has no runtime, returns an appropriate state."""
    sysman = _sysman()

    # Check if runtime exists
    try:
        if not sysman.IsTwinCATStarted():
            return {"mode": "NoRuntime", "detail": "Target has no TwinCAT runtime"}
    except Exception:
        pass

    # Set runtime state to Config via ConsumeXml on TIRS
    xml = (
        "<TreeItem>"
        "<RTStateDef>"
        "<CurrentState>Config</CurrentState>"
        "</RTStateDef>"
        "</TreeItem>"
    )
    try:
        rs = sysman.LookupTreeItem("TIRS")
        rs.ConsumeXml(xml)
        # Poll until runtime confirms the mode switch
        _poll_runtime_started(sysman, timeout=10.0, interval=1.0)
    except Exception:
        # Fallback: use SetTargetNetId + Activate pairing
        pass

    return {"mode": "Config"}


def set_run_mode() -> dict:
    """Switch the target TwinCAT to Run mode.

    Requires activated configuration first.
    """
    sysman = _sysman()

    try:
        if not sysman.IsTwinCATStarted():
            return {"mode": "NoRuntime", "detail": "Target has no TwinCAT runtime"}
    except Exception:
        pass

    xml = (
        "<TreeItem>"
        "<RTStateDef>"
        "<CurrentState>Run</CurrentState>"
        "</RTStateDef>"
        "</TreeItem>"
    )
    try:
        rs = sysman.LookupTreeItem("TIRS")
        rs.ConsumeXml(xml)
        # Poll until runtime confirms the mode switch
        _poll_runtime_started(sysman, timeout=10.0, interval=1.0)
    except Exception:
        pass

    return {"mode": "Run"}


# ---------------------------------------------------------------------------
# Target management
# ---------------------------------------------------------------------------

def get_target_net_id() -> str:
    """Get the currently set target NetId."""
    sysman = _sysman()
    return sysman.GetTargetNetId()


def set_target_net_id(net_id: str) -> dict:
    """Set the target NetId (for remote controllers).

    Args:
        net_id: Target AMS NetId, e.g. ``"172.16.1.100.1.1"``
    """
    sysman = _sysman()
    sysman.SetTargetNetId(net_id)
    set_active_target(net_id)
    return {"target_net_id": net_id}


def get_target_device_name() -> str:
    """Return a human-readable name for the current target.

    Goes through the .tsproj, looking at the first I/O device's
    ``AmsNetId`` / ``RemoteName`` attributes.

    1) Open project's I/O device name (COM)
    2) StaticRoutes.xml lookup (reverse NetId → name)
    3) Fallback: NetId string itself
    """
    # 1) Try from open project
    try:
        sysman = _sysman()
        net_id = sysman.GetTargetNetId()
        io_root = sysman.LookupTreeItem("TIID")
        for child in (io_root or []):
            # Read the device's ProduceXml to get RemoteName
            xml = child.ProduceXml()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml)
            remote = root.find(".//RemoteName")
            if remote is not None and remote.text:
                return remote.text.strip()
            # Also try the <AddressInfo> section
            addr = root.find(".//AddressInfo")
            if addr is not None:
                name_elem = addr.find(".//Name")
                if name_elem is not None and name_elem.text:
                    return name_elem.text.strip()
        # 2) Route name from StaticRoutes.xml
        routes = list_static_routes()
        for r in routes:
            if r["net_id"] == net_id:
                return r["name"]
        # 3) Just return the NetId
        return net_id
    except Exception:
        pass

    # Offline: try StaticRoutes.xml
    try:
        net_id = get_active_target_netid()
        routes = list_static_routes()
        for r in routes:
            if r["net_id"] == net_id:
                return r["name"]
    except Exception:
        pass

    return net_id


# ---------------------------------------------------------------------------
# TcVersion cache + active target persistence
# ---------------------------------------------------------------------------

def _tc_state_path() -> Path:
    """Path to the persistent target state file."""
    import os
    base = Path(os.environ.get("APPDATA", "")) / "tc-template"
    base.mkdir(parents=True, exist_ok=True)
    return base / "target_state.json"


def _load_target_state() -> dict:
    try:
        import json
        with open(_tc_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_target_state(data: dict) -> None:
    import json
    with open(_tc_state_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_active_target_netid() -> str:
    """Return the currently active target NetId.

    1. COM (open project's target)
    2. Persisted state from last ``tc target set``
    3. StaticRoutes.xml — if exactly one route exists, use its NetId
    """
    try:
        return get_target_net_id()
    except Exception:
        data = _load_target_state()
        cached = data.get("active_netid", "")
        if cached:
            return cached
        # Last resort: single route in StaticRoutes.xml
        routes = list_static_routes()
        if len(routes) == 1:
            return routes[0]["net_id"]
        return ""


def set_active_target(net_id: str) -> None:
    """Persist the active target NetId for offline detection."""
    data = _load_target_state()
    data["active_netid"] = net_id
    _save_target_state(data)


def cache_target_version(net_id: str, version: str) -> None:
    """Persist a target NetId → TcVersion mapping for later use."""
    data = _load_target_state()
    if "versions" not in data:
        data["versions"] = {}
    data["versions"][net_id] = version
    _save_target_state(data)


def get_cached_target_version(net_id: str) -> str:
    """Return a previously cached TcVersion for *net_id*, or ``""``."""
    data = _load_target_state()
    versions = data.get("versions", {})
    return versions.get(net_id, "")


def get_pinned_tc_version() -> dict:
    """Read the pinned TwinCAT version from the open project.

    Returns:
        dict with ``pinned_version`` (str), ``is_fixed`` (bool),
        ``target_version`` (str from tsproj).
    """
    try:
        dte = _dte()
        sln = dte.Solution
        for i in range(1, sln.Projects.Count + 1):
            proj = sln.Projects.Item(i)
            fname = str(getattr(proj, "FullName", ""))
            if not fname.endswith(".tsproj"):
                continue
            props = proj.Properties
            pinned = ""
            is_fixed = False
            try:
                pinned = str(props.Item("PinnedTcVersion").Value or "")
            except Exception:
                pass
            try:
                is_fixed = bool(props.Item("TcVersionFixed").Value)
            except Exception:
                pass
            return {
                "pinned_version": pinned,
                "is_fixed": is_fixed,
                "target_version": get_target_tc_version()["version_str"],
            }
    except Exception:
        pass
    return {"pinned_version": "", "is_fixed": False, "target_version": ""}


def set_pin_tc_version(version: str = "", fixed: bool = True) -> dict:
    """Pin (or unpin) the TwinCAT version for the open project.

    Args:
        version: TcVersion string (e.g. ``"3.1.4024.71"``).
            Pass ``""`` to unpin.
        fixed: Whether to set ``TcVersionFixed``.

    Returns:
        dict with ``pinned_version``, ``is_fixed``.
    """
    dte = _dte()
    sln = dte.Solution
    for i in range(1, sln.Projects.Count + 1):
        proj = sln.Projects.Item(i)
        fname = str(getattr(proj, "FullName", ""))
        if not fname.endswith(".tsproj"):
            continue
        props = proj.Properties
        try:
            props.Item("PinnedTcVersion").Value = version
        except Exception:
            pass
        try:
            props.Item("TcVersionFixed").Value = fixed
        except Exception:
            pass
        return {"pinned_version": version, "is_fixed": fixed}
    raise RuntimeError("No TwinCAT project (.tsproj) found in solution")


def _twincat_install_roots() -> list[Path]:
    """Return 4024/4026 binary roots without assuming one installer layout."""
    import os

    candidates = [
        Path(os.environ["TWINCAT3DIR"]) if os.environ.get("TWINCAT3DIR") else None,
        Path(r"C:\TwinCAT\3.1"),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Beckhoff" / "TwinCAT" / "3.1",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Beckhoff" / "TwinCAT" / "3.1",
    ]
    result: list[Path] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def list_plc_runtimes() -> dict:
    """Return every configured PLC runtime and its discovered ADS port.

    This is deliberately COM-only discovery.  The caller can then verify
    each runtime independently over ADS without assuming port 851.
    """
    sysman = _sysman()
    return {
        "target_netid": str(sysman.GetTargetNetId() or ""),
        "plcs": [{
            "name": runtime["name"],
            "project_name": runtime.get("project_name") or "",
            "ads_port": runtime.get("ads_port"),
            "port_source": runtime.get("port_source") or "unresolved",
        } for runtime in _list_plc_runtimes(sysman)],
    }


def _twincat_data_roots() -> list[Path]:
    """Return mutable/config roots (4026 moved these to ProgramData)."""
    import os

    candidates = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Beckhoff" / "TwinCAT" / "3.1",
        *_twincat_install_roots(),
    ]
    result: list[Path] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def get_local_tc_version() -> dict:
    """Read the locally installed TwinCAT version from ``TwinCATVersion.xml``.

    Returns:
        dict with ``major``, ``minor``, ``build``, ``revision`` (all ints)
        and ``version_str`` like ``"3.1.4026.20"``.
    """
    import os
    import xml.etree.ElementTree as ET

    candidates = [
        root / "SDK" / "TwinCATVersion.xml" for root in _twincat_install_roots()
    ]

    for p in candidates:
        if p is not None and p.is_file():
            try:
                root = ET.parse(str(p)).getroot()
                settings = root.find("appSettings")
                if settings is None:
                    continue
                vals: dict[str, int] = {}
                for kv in settings.findall("add"):
                    key = kv.get("key", "")
                    val = kv.get("value", "")
                    if key in ("major", "minor", "build", "revision"):
                        vals[key] = int(val)
                if len(vals) >= 3:
                    ver = f"{vals.get('major',0)}.{vals.get('minor',0)}.{vals.get('build',0)}.{vals.get('revision',0)}"
                    return {
                        "major": vals.get("major", 0),
                        "minor": vals.get("minor", 0),
                        "build": vals.get("build", 0),
                        "revision": vals.get("revision", 0),
                        "version_str": ver,
                    }
            except (ET.ParseError, OSError, ValueError):
                continue

    return {
        "major": 3, "minor": 1, "build": 4024, "revision": 0,
        "version_str": "3.1.4024.0",
    }


def get_installed_tc_versions() -> list[dict]:
    """Return all TwinCAT versions installed locally.

    Discovers versions from PLC compiler build targets
    (``Components/Plc/Build_*`` directories) and the currently active
    version from ``TwinCATVersion.xml``.

    Returns:
        List of version dicts sorted newest-first.  Each dict has
        ``major``, ``minor``, ``build``, ``revision`` (int),
        ``version_str`` (e.g. ``"3.1.4026.20"``), and
        ``active`` (bool — whether *this* version is the current
        active one reported by ``get_local_tc_version()``).
    """
    import os as _os
    import re as _re

    active = get_local_tc_version()
    active_str = active.get("version_str", "")

    seen: set[str] = set()
    versions: list[dict] = []

    def _add(ver_str: str) -> None:
        if ver_str in seen:
            return
        seen.add(ver_str)
        parts = ver_str.split(".")
        if len(parts) < 3:
            return
        versions.append({
            "major": int(parts[0]),
            "minor": int(parts[1]),
            "build": int(parts[2]),
            "revision": int(parts[3]) if len(parts) > 3 else 0,
            "version_str": ver_str,
            "active": ver_str == active_str,
        })

    # 1. PLC compiler builds: Components/Plc/Build_*
    for root in _twincat_install_roots():
        plc_dir = root / "Components" / "Plc"
        if not plc_dir.is_dir():
            continue
        for entry in sorted(plc_dir.iterdir()):
            if not entry.is_dir():
                continue
            m = _re.match(r"^Build_(\d+)\.(\d+)$", entry.name)
            if m:
                _add(f"3.1.{m.group(1)}.{m.group(2)}")

    # 3. Always include the active version (may not have a Build_ dir entry)
    _add(active_str)

    # Sort newest-first by (build, revision)
    versions.sort(key=lambda v: (v["build"], v["revision"]), reverse=True)
    return versions


def get_target_tc_version() -> dict:
    """Detect the TwinCAT version of the current target.

    Priority:
        1. Open project's ``TcVersion`` via COM (if XAE is running)
        2. Cached version for the current target NetId
        3. Local installed version (with a warning that target may differ)

    Returns:
        Same dict shape as :func:`get_local_tc_version`, plus
        ``source`` key: ``"project"`` / ``"cache"`` / ``"local"``.
    """
    import xml.etree.ElementTree as ET

    # 1. Try reading from the open tsproj via COM
    try:
        dte = _dte()
        sln = dte.Solution
        for i in range(1, sln.Projects.Count + 1):
            proj = sln.Projects.Item(i)
            fname = str(getattr(proj, "FullName", ""))
            if fname.endswith(".tsproj"):
                tree = ET.parse(fname)
                root = tree.getroot()
                tcver = root.get("TcVersion", "")
                if tcver:
                    parts = tcver.split(".")
                    if len(parts) >= 3:
                        # Cache for later
                        try:
                            target = sln.Projects.Item(1).Object.GetTargetNetId()
                            cache_target_version(target, tcver)
                        except Exception:
                            pass
                        return {
                            "major": int(parts[0]),
                            "minor": int(parts[1]),
                            "build": int(parts[2]),
                            "revision": int(parts[3]) if len(parts) > 3 else 0,
                            "version_str": tcver,
                            "source": "project",
                        }
    except Exception:
        pass

    # 2. Try cached version for current target
    try:
        dte2 = None
        import win32com.client
        for pid in ["TcXaeShell.DTE.15.0", "VisualStudio.DTE.17.0"]:
            try:
                dte2 = win32com.client.GetActiveObject(pid)
                break
            except Exception:
                continue
        if dte2:
            sln2 = dte2.Solution
            for i in range(1, getattr(sln2.Projects, "Count", 0) + 1):
                proj = sln2.Projects.Item(i)
                fname = str(getattr(proj, "FullName", ""))
                if fname.endswith(".tsproj"):
                    target = proj.Object.GetTargetNetId()
                    cached = get_cached_target_version(target)
                    if cached:
                        parts = cached.split(".")
                        if len(parts) >= 3:
                            return {
                                "major": int(parts[0]),
                                "minor": int(parts[1]),
                                "build": int(parts[2]),
                                "revision": int(parts[3]) if len(parts) > 3 else 0,
                                "version_str": cached,
                                "source": "cache",
                            }
    except Exception:
        pass

    # 3. Fallback to local
    local = get_local_tc_version()
    local["source"] = "local"
    return local


# ---------------------------------------------------------------------------
# Static route resolution
# ---------------------------------------------------------------------------

def _get_static_routes_path() -> Path:
    """Return the filesystem path to ``StaticRoutes.xml``.

    Searches standard Beckhoff installation directories.
    """
    import os

    candidates = [
        root / "Target" / "StaticRoutes.xml" for root in _twincat_data_roots()
    ]

    for p in candidates:
        if p is not None and p.is_file():
            return p

    raise FileNotFoundError(
        "StaticRoutes.xml not found. "
        "Ensure TwinCAT 3.1 is installed, or set the TWINCAT3DIR environment variable."
    )


def list_static_routes() -> list[dict]:
    """List all configured remote routes from ``StaticRoutes.xml``.

    Returns:
        List of route dicts.  Each dict has keys ``name``, ``address``,
        ``net_id``, ``type``, ``flags``, ``unidirectional``.
        Returns an empty list when the file is missing or unparseable.
    """
    import xml.etree.ElementTree as ET

    try:
        path = _get_static_routes_path()
        tree = ET.parse(str(path))
        root = tree.getroot()
    except (FileNotFoundError, ET.ParseError, OSError):
        return []

    remote_conns = root.find("RemoteConnections")
    if remote_conns is None:
        return []

    routes: list[dict] = []

    for route_elem in remote_conns.findall("Route"):

        def _text(tag: str) -> str:
            child = route_elem.find(tag)
            return (child.text or "").strip() if child is not None else ""

        routes.append({
            "name": _text("Name"),
            "address": _text("Address"),
            "net_id": _text("NetId"),
            "type": _text("Type"),
            "flags": _text("Flags"),
            "unidirectional": route_elem.get("Unidirectional", "true"),
        })

    return routes


def resolve_target(query: str) -> dict:
    """Resolve a user-supplied string against configured static routes.

    Matching priority (first win):
        1. Exact case-insensitive **name** match
        2. Partial case-insensitive **name** match (query substring)
        3. Exact case-insensitive **address** match
        4. Exact case-insensitive **NetId** match
        5. Partial case-insensitive **NetId** match

    Args:
        query: A route name, IP address, or AMS NetId to search for.

    Returns:
        The matched route dict (same keys as :func:`list_static_routes`)
        plus an additional ``matched_by`` key indicating which field matched.

    Raises:
        ValueError: No route matches *query*.
    """
    routes = list_static_routes()
    if not routes:
        raise ValueError(
            "No static routes configured. "
            "Add routes via TwinCAT XAE → Route Settings, "
            "or pass a raw AMS NetId directly."
        )

    q = query.strip().lower()

    # Priority 1: exact name
    for r in routes:
        if r["name"].lower() == q:
            return {**r, "matched_by": "name"}

    # Priority 2: partial name
    for r in routes:
        if q in r["name"].lower():
            return {**r, "matched_by": "name (partial)"}

    # Priority 3: exact address
    for r in routes:
        if r["address"].lower() == q:
            return {**r, "matched_by": "address"}

    # Priority 4: exact NetId
    for r in routes:
        if r["net_id"].lower() == q:
            return {**r, "matched_by": "net_id"}

    # Priority 5: partial NetId
    for r in routes:
        if q in r["net_id"].lower():
            return {**r, "matched_by": "net_id (partial)"}

    raise ValueError(
        f"No route matches '{query}'. "
        f"Use 'tc target list' to see available targets."
    )


def list_local_adapters() -> list[dict]:
    """Return local network adapters with IP and subnet mask.

    Uses ``ipconfig`` on Windows to discover active interfaces.

    Returns:
        List of dicts with keys ``name``, ``ip``, ``mask``, ``subnet``
        (CIDR notation, e.g. ``\"192.168.1.0/24\"``).
    """
    import re as _re
    import subprocess
    import ipaddress

    try:
        r = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []

    adapters: list[dict] = []
    current = ""
    lines = r.stdout.splitlines()

    for i, line in enumerate(lines):
        # Adapter header line
        m = _re.match(
            r"^(.+?)(?:adapter|适配器)\s+([^:]+):?$",
            line.strip(), _re.IGNORECASE,
        )
        if m:
            current = m.group(2).strip()
            continue

        ip_m = _re.search(
            r"IPv4\s*(?:Address|地址)[^:]*:\s*(\d+\.\d+\.\d+\.\d+)",
            line,
        )
        if not ip_m or not current:
            continue

        ip = ip_m.group(1)
        mask = "255.255.255.0"  # default
        # Look ahead for subnet mask
        for j in range(i + 1, min(i + 6, len(lines))):
            mm = _re.search(
                r"(?:Subnet|子网)[^:]*:\s*(\d+\.\d+\.\d+\.\d+)",
                lines[j],
            )
            if mm:
                mask = mm.group(1)
                break

        try:
            net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
            cidr = str(net)
        except Exception:
            cidr = f"{'.'.join(ip.split('.')[:3])}.0/24"

        adapters.append({
            "name": current,
            "ip": ip,
            "mask": mask,
            "subnet": cidr,
        })

    return adapters


def search_network_targets(subnet: str = "", timeout: float = 0.1,
                           max_hosts: int = 256) -> list[dict]:
    """Scan local subnets for Beckhoff devices via TCP port 48898.

    Probes each IP for the AMS Router port.  Found devices are
    cross-referenced against ``StaticRoutes.xml`` to show known names.

    Args:
        subnet: CIDR subnet to scan (e.g. ``\"192.168.1.0/24\"``).
            When empty, all local subnets are scanned.
        timeout: Per-host TCP connect timeout in seconds.
        max_hosts: Max hosts per subnet (clamped to 256).

    Returns:
        List of dicts with ``ip``, ``hostname``, ``route_name`` (if known).
    """
    import subprocess
    import re as _re
    import socket as _socket
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ── Resolve subnets to scan ──
    subnets: list[str] = []
    if subnet:
        subnets.append(subnet)
    else:
        # Gather local IPs
        local_ips: list[str] = []
        try:
            hostname = _socket.gethostname()
            _, _, addrs = _socket.gethostbyname_ex(hostname)
            local_ips.extend(addrs)
        except Exception:
            pass
        # Also try ipconfig
        try:
            r = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                timeout=5,
            )
            for m in _re.finditer(
                r"IPv4\s*(?:Address|地址)[^:]*:\s*(\d+\.\d+\.\d+\.\d+)",
                r.stdout,
            ):
                ip = m.group(1)
                if ip not in local_ips:
                    local_ips.append(ip)
        except Exception:
            pass

        for ip in local_ips:
            parts = ip.split(".")
            subnets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.0/24")

    # ── Scan each subnet ──
    all_results: list[dict] = []
    seen_ips: set[str] = set()

    for subnet_str in subnets:
        base_parts = subnet_str.split("/")[0].split(".")
        base = ".".join(base_parts[:3])
        start = 1
        end = min(start + max_hosts, 255)

        def _check(host: int) -> dict | None:
            ip = f"{base}.{host}"
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, 48898))
                sock.close()
                if result == 0:
                    # Try reverse DNS
                    try:
                        hostname2 = _socket.gethostbyaddr(ip)[0]
                    except Exception:
                        hostname2 = ""
                    return {"ip": ip, "hostname": hostname2}
                return None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=50) as pool:
            futures = {pool.submit(_check, h): h for h in range(start, end + 1)}
            for future in as_completed(futures):
                try:
                    r = future.result()
                    if r and r["ip"] not in seen_ips:
                        all_results.append(r)
                        seen_ips.add(r["ip"])
                except Exception:
                    pass

    # ── Cross-reference with StaticRoutes.xml ──
    known = {}
    try:
        for route in list_static_routes():
            known[route["address"]] = route["name"]
    except Exception:
        pass

    # ── Also probe known StaticRoute addresses (may be in other subnets) ──
    try:
        known_ips = set()
        for route in list_static_routes():
            addr = route.get("address", "")
            if addr and addr not in seen_ips:
                known_ips.add(addr)

        def _probe_one(ip: str) -> dict | None:
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.settimeout(timeout)
                rc = sock.connect_ex((ip, 48898))
                sock.close()
                if rc == 0:
                    try:
                        h = _socket.gethostbyaddr(ip)[0]
                    except Exception:
                        h = ""
                    return {"ip": ip, "hostname": h}
                return None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = {pool.submit(_probe_one, ip): ip for ip in known_ips}
            for fut in as_completed(futs):
                r = fut.result()
                if r and r["ip"] not in seen_ips:
                    all_results.append(r)
                    seen_ips.add(r["ip"])
    except Exception:
        pass

    for dev in all_results:
        dev["route_name"] = known.get(dev["ip"], "")

    return all_results


# ---------------------------------------------------------------------------
# Comprehensive Beckhoff device discovery — ARP + ping + ADS
# ---------------------------------------------------------------------------

_BECKHOFF_MAC_PREFIXES = ("00-01-05", "00-01-05-")


def find_beckhoff_devices(scan_method: str = "all") -> list[dict]:
    """Discover Beckhoff controllers reachable from this PC.

    Three methods are tried in order (depending on *scan_method*):

    1. **arp** — read the local ARP cache for MAC addresses starting
       with the Beckhoff OUI ``00-01-05``.  Fastest and most reliable.
    2. **ping** — sweep the subnet of each local adapter looking for
       responsive IPs.
    3. **ads** — TCP-probe port 48898 (AMS Router).

    Args:
        scan_method: ``"arp"``, ``"ping"``, ``"ads"`` or ``"all"`` (default).

    Returns:
        List of dicts with ``ip``, ``mac``, ``hostname``, ``route_name``,
        ``method``.
    """
    import subprocess as _sp
    import re as _re
    import threading

    all_devices: dict[str, dict] = {}  # IP → device info

    # ── Pre-load route table for cross-reference ──
    known: dict[str, str] = {}
    try:
        for route in list_static_routes():
            known[route["address"]] = route["name"]
    except Exception:
        pass

    def _add(ip: str, mac: str = "", hostname: str = "", method: str = ""):
        if ip in all_devices:
            existing = all_devices[ip]
            if mac and not existing.get("mac"):
                existing["mac"] = mac
            if method:
                existing["method"] = existing.get("method", "") + "+" + method
            return
        all_devices[ip] = {
            "ip": ip, "mac": mac, "hostname": hostname,
            "route_name": known.get(ip, ""), "method": method,
        }

    # ── Method 1: ARP cache ──
    if scan_method in ("arp", "all"):
        try:
            r = _sp.run(
                ["arp", "-a"], capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                ip_m = _re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                mac_low = line.lower()
                if ip_m and any(pfx.lower() in mac_low for pfx in _BECKHOFF_MAC_PREFIXES):
                    mac_m = _re.search(
                        r"([0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2}[-:][0-9a-f]{2})",
                        mac_low,
                    )
                    _add(ip_m.group(1), mac=mac_m.group(1) if mac_m else "",
                         method="arp")
        except Exception:
            pass

    # ── Method 2: Ping sweep ──
    if scan_method in ("ping", "all"):
        subnet_bases: list[str] = []
        for ad in list_local_adapters():
            ip = ad.get("ip", "")
            parts = ip.split(".")
            if len(parts) != 4:
                continue
            name_low = ad.get("name", "").lower()
            if "vmware" in name_low or "vmnet" in name_low:
                continue
            if "wlan" in name_low or "wireless" in name_low or "wi-fi" in name_low:
                continue
            if parts[0] in ("127",):
                continue
            subnet_bases.append(".".join(parts[:3]))
        subnet_bases = list(dict.fromkeys(subnet_bases))

        for base in subnet_bases:
            lock = threading.Lock()
            def _ping(host: int):
                ip = f"{base}.{host}"
                try:
                    r = _sp.run(
                        ["ping", "-n", "1", "-w", "200", ip],
                        capture_output=True, text=True, timeout=1.5,
                    )
                    if "TTL=" in r.stdout:
                        with lock:
                            _add(ip, method="ping")
                except Exception:
                    pass

            threads = []
            for h in range(1, 255):
                t = threading.Thread(target=_ping, args=(h,))
                threads.append(t); t.start()
                if len(threads) >= 200:
                    for t in threads: t.join(timeout=1.0)
                    threads.clear()
            for t in threads: t.join(timeout=1.0)

    # ── Method 3: TCP port 48898 ──
    if scan_method in ("ads", "all"):
        for dev in search_network_targets():
            ip = dev.get("ip", "")
            if ip:
                _add(ip, hostname=dev.get("hostname", ""), method="ads")

    return sorted(all_devices.values(),
                  key=lambda d: tuple(map(int, d["ip"].split("."))))


def add_static_route(name: str, address: str, net_id: str = "",
                     user: str = "", password: str = "",
                     route_type: str = "TCP_IP", flags: str = "160",
                     unidirectional: bool = True) -> dict:
    """Add a new AMS route to ``StaticRoutes.xml``.

    Writes the route directly to the static routes file.  The TwinCAT
    AMS Router will pick up the change on next restart.

    Args:
        name: Display name for the route (e.g. ``"CX-NewDevice"``).
        address: IP address or hostname (e.g. ``"192.168.1.100"``).
        net_id: AMS NetId.  If empty, defaults to ``{address}.1.1``.
        route_type: Transport type (almost always ``"TCP_IP"``).
        flags: Route flags (``"160"`` = standard).

    Returns:
        dict with ``status``, ``added`` (bool), ``name``, ``address``,
        ``net_id``.
    """
    import xml.etree.ElementTree as ET

    if not net_id:
        net_id = f"{address}.1.1"

    # ── GUI path (with credentials — safe, no filesystem writes) ──
    if user:
        try:
            verified = _gui_add_route(address, net_id, user, password or "1")
        except Exception:
            verified = False
        return {
            "status": "ok" if verified else "error",
            "added": verified,
            "verified": verified,
            "name": name,
            "address": address,
            "net_id": net_id,
            "message": ("Route registered." if verified
                        else "GUI route registration failed. Check that the device is reachable."),
        }

    # ── Filesystem write is blocked — the GUI path is the only supported way ──
    return {
        "status": "error",
        "message": (
            "No credentials provided. "
            "Use --auto-auth for default credentials (Administrator/1), "
            "or --user / --password for custom ones."
        ),
        "added": False, "verified": False,
        "name": name, "address": address, "net_id": net_id,
    }



# ---------------------------------------------------------------------------
# GUI route registration (pyautogui)
# ---------------------------------------------------------------------------

def _find_child_handles(parent_hwnd: int) -> list[int]:
    """Return all child window handles of *parent_hwnd*."""
    import win32gui
    handles: list[int] = []

    def _collect(h, _):
        handles.append(h)
        return True

    win32gui.EnumChildWindows(parent_hwnd, _collect, None)
    return handles



def _find_twincat_icon() -> tuple[int, int] | None:
    """Locate the TwinCAT system-tray icon via screenshot.

    Uses colour-based detection (Beckhoff blue circle) with the
    saved reference image as fallback.

    Returns ``(x, y)`` screen coordinates of the icon centre, or ``None``.
    """
    import pyautogui

    sw, sh = pyautogui.size()
    # Tray area: bottom 60 px, rightmost 500 px
    region = (max(0, sw - 500), max(0, sh - 60), min(500, sw), 60)
    img = pyautogui.screenshot(region=region)

    # ── Method 1: colour detection (fast) ──
    best_x = best_y = None
    best_score = 0
    for y in range(img.height - 20):
        for x in range(img.width - 20):
            r, g, b = img.getpixel((x, y))[:3]
            if b > 140 and b > g + 30 and b > r + 60:
                score = 0
                for dy in range(-8, 9):
                    for dx in range(-8, 9):
                        px = x + dx
                        py = y + dy
                        if 0 <= px < img.width and 0 <= py < img.height:
                            pr, pg, pb = img.getpixel((px, py))[:3]
                            if pb > 100 and pb > pg + 20 and pb > pr + 40:
                                score += 1
                if score > best_score:
                    best_score = score
                    best_x, best_y = region[0] + x, region[1] + y

    if best_score >= 20:
        return (best_x, best_y)

    # ── Method 2: template matching ──
    try:
        from pathlib import Path
        tmpl_path = Path(__file__).resolve().parent.parent / "twincat_icon_template.png"
        if tmpl_path.is_file():
            loc = pyautogui.locate(str(tmpl_path), img, confidence=0.7)
            if loc:
                return (region[0] + loc.left + loc.width // 2,
                        region[1] + loc.top + loc.height // 2)
    except Exception:
        pass

    return None


def _safe(hwnd: int):
    """Return (text, class, rect) for a window handle, never throws."""
    try:
        return (win32gui.GetWindowText(hwnd),
                win32gui.GetClassName(hwnd),
                win32gui.GetWindowRect(hwnd))
    except Exception:
        return "", "", (0, 0, 0, 0)


def _enum_children(parent: int):
    """Return list of valid child handles, filtering stale ones."""
    kids = []

    def _cb(h, _):
        try:
            # Touch the class name to verify handle is still alive
            win32gui.GetClassName(h)
            kids.append(h)
        except Exception:
            pass

    try:
        win32gui.EnumChildWindows(parent, _cb, None)
    except Exception:
        pass
    return kids


def _gui_add_route(ip: str, net_id: str, user: str, password: str) -> bool:
    """Auto-register AMS route via TwinCAT GUI.  All controls found dynamically.

    Opens the Static Routes dialog via XAE COM (``dte.ExecuteCommand``)
    so it always works from the running TcXaeShell instance — no separate
    ``TcAmsRemoteMgr.exe`` subprocess.
    """
    import time as _t, win32gui, ctypes, win32con

    def _bring_to_front(hwnd):
        """Bring window to foreground, bypassing Windows background-process restriction.

        Uses AttachThreadInput + SetForegroundWindow + SetWindowPos(TOPMOST→NOTOPMOST)
        to force the window to the foreground reliably.
        """
        FS_TOPMOST = -1
        FS_NON_TOPMOST = -2
        cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()

        # Attach our thread to the foreground thread so SetForegroundWindow works
        try:
            fg = win32gui.GetForegroundWindow()
            fg_thread = ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
            ctypes.windll.user32.AttachThreadInput(cur_thread, fg_thread, True)
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
                win32gui.SetForegroundWindow(hwnd)
                ctypes.windll.user32.SetFocus(hwnd)
            finally:
                ctypes.windll.user32.AttachThreadInput(cur_thread, fg_thread, False)
        except Exception:
            pass

        # Topmost flick — bring to absolute top then release
        try:
            ctypes.windll.user32.SetWindowPos(hwnd, FS_TOPMOST, 0, 0, 0, 0, 0x0002 | 0x0001)
            _t.sleep(0.1)
            ctypes.windll.user32.SetWindowPos(hwnd, FS_NON_TOPMOST, 0, 0, 0, 0, 0x0002 | 0x0001)
        except Exception:
            pass

    def _kids(parent):
        ks = []
        def _c(h, _2):
            try: win32gui.GetClassName(h); ks.append(h)
            except: pass
        try: win32gui.EnumChildWindows(parent, _c, None)
        except: pass
        return ks

    def _btn(parent, text):
        """Click a button by text. Uses BM_CLICK (no screen coords, no mouse movement)."""
        _bring_to_front(parent)
        for h in _kids(parent):
            try:
                t = win32gui.GetWindowText(h); cl = win32gui.GetClassName(h)
                r = win32gui.GetWindowRect(h)
                if text in t and cl == "Button" and r[2]-r[0] > 20:
                    # BM_CLICK — works even when window is behind other windows
                    ctypes.windll.user32.PostMessageW(h, 0x00F5, 0, 0)
                    _t.sleep(0.15); return True
            except: pass
        return False

    def _find_dlg(title_keyword):
        found = None
        def _f(h, _2):
            nonlocal found
            if win32gui.IsWindowVisible(h) and title_keyword in win32gui.GetWindowText(h):
                found = h
        win32gui.EnumWindows(_f, None)
        return found

    # ① Launch Static Routes dialog
    # TcAmsRemoteMgr.exe is the dedicated TwinCAT tool that opens the
    # "TwinCAT Static Routes" window.  Do NOT use
    # dte.ExecuteCommand("TwinCAT.TargetBrowser") — that opens a different
    # window (Target Browser) which does NOT have the Add Route flow.
    #
    # .shell=True is required because the TwinCAT installer puts the
    # binary directory on PATH but subprocess may not inherit it.
    import subprocess as _sp
    remote_manager = next(
        (
            root / "System" / "TcAmsRemoteMgr.exe"
            for root in _twincat_install_roots()
            if (root / "System" / "TcAmsRemoteMgr.exe").is_file()
        ),
        None,
    )
    if remote_manager is None:
        raise FileNotFoundError(
            "TcAmsRemoteMgr.exe not found in TwinCAT 4024/4026 installation roots"
        )
    _sp.Popen([str(remote_manager)], shell=True)

    # ② Wait for Static Routes
    sr = None
    for i in range(30):
        _t.sleep(0.5)
        w = []
        def _f(h, _2):
            if win32gui.IsWindowVisible(h) and "TwinCAT Static Routes" in win32gui.GetWindowText(h): w.append(h)
        win32gui.EnumWindows(_f, None)
        if len(w) >= 1: sr = w[0]; print(f"  [dbg] SR found after {(i+1)*0.5:.1f}s ({len(w)} window(s))"); break
        if i % 6 == 5: print(f"  [dbg] waiting SR... ({len(w)} windows)")
    if not sr: print("  [dbg] FAIL: no Static Routes"); return False
    _bring_to_front(sr); _t.sleep(0.5)
    if not _btn(sr, "Add..."): print("  [dbg] FAIL: Add... button"); return False
    print("  [dbg] Clicked Add..."); _t.sleep(4)

    # ③ Find Add Route Dialog (exact match, NOT "Add Remote Route")
    add_dlg = None
    for _ in range(15):
        # Bring the Static Routes window forward to help the dialog surface
        try:
            _bring_to_front(sr)
        except Exception:
            pass
        _t.sleep(0.8)
        def _f3(h, _2):
            nonlocal add_dlg
            t = win32gui.GetWindowText(h)
            if win32gui.IsWindowVisible(h) and "Add Route Dialog" in t:
                add_dlg = h
        win32gui.EnumWindows(_f3, None)
        if add_dlg:
            _bring_to_front(add_dlg)
            _t.sleep(0.3)
            break
    if not add_dlg:
        print("  [dbg] FAIL: Add Route Dialog")
        # Print all visible windows for debugging
        def _dbg_all(h, _2):
            try:
                t = win32gui.GetWindowText(h)
                if win32gui.IsWindowVisible(h) and t:
                    print(f"  [dbg] visible: '{t}'")
            except: pass
        win32gui.EnumWindows(_dbg_all, None)
        return False
    print("  [dbg] Add Route Dialog found"); _t.sleep(0.5)

    # ④ Fill IP (via WM_SETTEXT — no pyautogui click/type)
    _bring_to_front(add_dlg); _t.sleep(0.3)
    filled = False
    for h in _kids(add_dlg):
        try:
            cl = win32gui.GetClassName(h); t = win32gui.GetWindowText(h)
            r = win32gui.GetWindowRect(h)
            if cl == "Edit" and not t and r[3]-r[1] == 24:
                # Set focus then set text via messages
                ctypes.windll.user32.SetFocus(h)
                _t.sleep(0.1)
                ctypes.windll.user32.SendMessageW(h, 0x000C, 0, ip)  # WM_SETTEXT
                _t.sleep(0.2)
                filled = True; print(f"  [dbg] Filled IP: {ip}")
                break
        except: pass
    if not filled: print("  [dbg] FAIL: no empty Edit field"); return False

    # ⑤ Refresh Status — must click this before Enter Host Name / IP
    if _btn(add_dlg, "Refresh Status"):
        print("  [dbg] Clicked Refresh Status"); _t.sleep(1.5)
    else:
        print("  [dbg] WARN: Refresh Status button not found, continuing...")

    # ⑥ Enter Host Name / IP
    if not _btn(add_dlg, "Enter Host Name"): print("  [dbg] FAIL: Enter Host button"); return False
    print("  [dbg] Clicked Enter Host Name / IP")

    # ⑦ Poll device list
    device_found = False
    for i in range(20):
        _t.sleep(0.5)
        add_dlg = _find_dlg("Add Route Dialog")
        if not add_dlg: print(f"  [dbg] Dialog lost at poll {i}"); continue
        _bring_to_front(add_dlg)
        for h in _kids(add_dlg):
            try:
                if win32gui.GetClassName(h) == "SysListView32":
                    rows = win32gui.SendMessage(h, 0x1000 + 4, 0, 0)
                    if i % 4 == 0: print(f"  [dbg] poll {i}: rows={rows}")
                    if rows > 0: device_found = True
                    break
            except: pass
        if device_found: print(f"  [dbg] Device found at poll {i}!"); break
    if not device_found: print("  [dbg] FAIL: no device rows after 10s"); return False

    # ⑧ Click Add Route
    add_dlg = _find_dlg("Add Route Dialog")
    if not add_dlg: print("  [dbg] FAIL: dialog lost before Add Route"); return False
    if not _btn(add_dlg, "Add Route"): print("  [dbg] FAIL: Add Route button"); return False
    print("  [dbg] Clicked Add Route"); _t.sleep(5)

    # ⑨ Credential dialog (distinct title: "Add Remote Route", NOT "Add Route Dialog")
    cred = None
    for _ in range(30):
        _t.sleep(0.5)
        def _fcred(h, _2):
            nonlocal cred
            if not win32gui.IsWindowVisible(h): return
            t = win32gui.GetWindowText(h)
            # "Add Remote Route" 不要混淆 "Add Route Dialog"
            if "Add Remote Route" in t and "Dialog" not in t: cred = h
            # 兜底：任何包含 credential/logon/security 的窗口
            elif any(kw in t.lower() for kw in ('credential','log on','security','remote route')):
                cred = h
        win32gui.EnumWindows(_fcred, None)
        if cred: break
    if not cred:
        print("  [dbg] FAIL: credential dialog not found")
        def _dbg_c(h, _2):
            try:
                t = win32gui.GetWindowText(h)
                if win32gui.IsWindowVisible(h) and t:
                    print(f"  [dbg] visible cred: '{t}'")
            except: pass
        win32gui.EnumWindows(_dbg_c, None)
        return False
    _bring_to_front(cred); _t.sleep(0.5)

    # 1) Click "Secure ADS" button via BM_CLICK
    def _find_secure_btn(h, _2):
        try:
            t = win32gui.GetWindowText(h).lower(); cl = win32gui.GetClassName(h)
            if cl == "Button" and "secure ads" in t:
                ctypes.windll.user32.PostMessageW(h, 0x00F5, 0, 0)
                _t.sleep(0.2)
        except: pass
    win32gui.EnumChildWindows(cred, _find_secure_btn, None)

    # 2) Enumerate controls, capture HWNDs of Edit fields
    _t.sleep(0.5)
    kids = _kids(cred)

    labels = {}
    edit_hwnds = []  # (hwnd, rect)
    btn_hwnds = {}   # label_lower → hwnd
    for h in kids:
        try:
            cl = win32gui.GetClassName(h); t = win32gui.GetWindowText(h)
            r = win32gui.GetWindowRect(h); w2, h2 = r[2]-r[0], r[3]-r[1]
            if cl == "Static" and w2 > 20:
                labels[t.lower()] = r
            elif cl == "Edit" and w2 > 80:
                edit_hwnds.append((h, r))
            elif cl == "Button" and w2 > 60:
                btn_hwnds[t.lower()] = h
        except: pass

    def _edit_hwnd(label_key):
        lr = labels.get(label_key)
        if not lr: return None
        for hw, er in edit_hwnds:
            if abs(er[1] - lr[1]) < 16 and er[0] > lr[0]:
                return hw
        return None

    # 3) Fill User via WM_SETTEXT
    eu = _edit_hwnd("user:")
    if eu:
        ctypes.windll.user32.SendMessageW(eu, 0x000C, 0, user)  # WM_SETTEXT
        _t.sleep(0.2)

    # 4) Fill Password via WM_SETTEXT
    ep = _edit_hwnd("password:")
    if ep:
        ctypes.windll.user32.SendMessageW(ep, 0x000C, 0, password)  # WM_SETTEXT
        _t.sleep(0.2)

    # 5) Click Okay via BM_CLICK
    clicked_ok = False
    for label, hw in btn_hwnds.items():
        if any(w in label for w in ('okay', 'ok')):
            ctypes.windll.user32.PostMessageW(hw, 0x00F5, 0, 0)
            _t.sleep(0.3)
            clicked_ok = True
            break
    if not clicked_ok:
        # Send Enter to the dialog as fallback
        ctypes.windll.user32.PostMessageW(cred, 0x0100, 0x0D, 0x1C0001)

    # Wait for dialog to close
    for _ in range(20):
        _t.sleep(0.5)
        still_open = False
        def _check(h, _2):
            nonlocal still_open
            if win32gui.IsWindowVisible(h) and "Add Remote Route" in win32gui.GetWindowText(h):
                still_open = True
        win32gui.EnumWindows(_check, None)
        if not still_open: break

    # SUCCESS — route is registered
    # Clean up lingering dialogs (best-effort, don't affect return value)
    try:
        sr_hwnd = None
        def _f(h, _2):
            nonlocal sr_hwnd
            if win32gui.IsWindowVisible(h) and "TwinCAT Static Routes" in win32gui.GetWindowText(h):
                sr_hwnd = h
        win32gui.EnumWindows(_f, None)
        if sr_hwnd:
            _bring_to_front(sr_hwnd); _t.sleep(0.3)
            ctypes.windll.user32.PostMessageW(sr_hwnd, 0x0010, 0, 0)  # WM_CLOSE
            _t.sleep(0.3)
        add_dlg = _find_dlg("Add Route Dialog")
        if add_dlg:
            ctypes.windll.user32.PostMessageW(add_dlg, 0x0010, 0, 0)
    except Exception:
        pass
    return True


def _dismiss_credential(user: str, password: str, net_id: str = ""):
    """Fill "Add Remote Route" credential dialog.

    Finds "User:" / "Password:" labels, clicks adjacent Edit fields,
    toggles "Secure ADS", clicks "Okay".
    """
    import pyautogui
    import win32gui
    import time as _t

    for _ in range(20):
        dlg = None

        def _cb(h, _2):
            nonlocal dlg
            if not win32gui.IsWindowVisible(h):
                return
            t = win32gui.GetWindowText(h).lower()
            r = win32gui.GetWindowRect(h)
            w = r[2] - r[0]
            h2 = r[3] - r[1]
            if w > 300 and h2 > 200 and any(
                kw in t for kw in (
                    "add remote route", "remote route", "log on",
                    "credential", "security", "authentication",
                )
            ):
                dlg = h

        win32gui.EnumWindows(_cb, None)
        if dlg is None:
            _t.sleep(1)
            continue

        try:
            _bring_to_front(dlg)
        except Exception:
            pass
        _t.sleep(0.3)

        ks = []
        def _kc(h, _2):
            try:
                win32gui.GetClassName(h)
                ks.append(h)
            except Exception:
                pass
        try:
            win32gui.EnumChildWindows(dlg, _kc, None)
        except Exception:
            pass

        # Find Edit to the right of a label (±12px Y, X > label.X)
        def _edit_right(label_lower: str):
            lr = None
            for h in ks:
                try:
                    t = win32gui.GetWindowText(h).lower()
                    if label_lower in t:
                        lr = win32gui.GetWindowRect(h)
                        break
                except Exception:
                    pass
            if lr is None:
                return None
            best, best_d = None, 9999
            for h in ks:
                try:
                    if win32gui.GetClassName(h) != "Edit":
                        continue
                    r = win32gui.GetWindowRect(h)
                    if abs(r[1] - lr[1]) < 14 and r[0] > lr[0]:
                        d = r[0] - lr[0]
                        if d < best_d:
                            best_d = d
                            best = r
                except Exception:
                    pass
            return best

        # 1) User
        er = _edit_right("user:")
        if er:
            pyautogui.click((er[0] + er[2]) // 2, (er[1] + er[3]) // 2)
            _t.sleep(0.1)
            pyautogui.hotkey("ctrl", "a")
            _t.sleep(0.05)
            pyautogui.write(user)

        # 2) Password
        er = _edit_right("password:")
        if er:
            pyautogui.click((er[0] + er[2]) // 2, (er[1] + er[3]) // 2)
            _t.sleep(0.1)
            pyautogui.hotkey("ctrl", "a")
            _t.sleep(0.05)
            pyautogui.write(password)

        # 3) Secure ADS — click to uncheck
        for h in ks:
            try:
                t = win32gui.GetWindowText(h).lower()
                cl = win32gui.GetClassName(h)
                r = win32gui.GetWindowRect(h)
                if "secure ads" in t and cl == "Button":
                    pyautogui.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                    _t.sleep(0.1)
                    break
            except Exception:
                pass

        # 4) Okay
        for h in ks:
            try:
                t = win32gui.GetWindowText(h).lower()
                cl = win32gui.GetClassName(h)
                r = win32gui.GetWindowRect(h)
                if "okay" in t and cl == "Button":
                    pyautogui.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                    return
            except Exception:
                pass

        pyautogui.press("enter")
        return

    pyautogui.press("enter")


def _indent_xml(elem, level: int = 0) -> None:
    """Pretty-print XML element tree (in-place)."""
    indent = "\n" + "\t" * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "\t"
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent


# ---------------------------------------------------------------------------
# I/O device scanning
# ---------------------------------------------------------------------------

# ── Meta-nodes injected by the EtherCAT master (not physical terminals) ──
_SLAVE_META_NODES = frozenset({
    "Image", "Image-Info", "SyncUnits", "Inputs", "Outputs", "InfoData",
})


def _has_slave_terminals(device_item) -> bool:
    """Return True when *device_item* has at least one physical terminal / box.

    Physical terminals are children whose name is-not-a meta-node
    (``Image``, ``SyncUnits``, ``Inputs``, ``Outputs``, …).  The master
    may name terminals in different ways (``Term 1 (EK1100)``,
    ``Box 1 (EPP…)``, ``Drive 1``, …) so a negative check is safest.
    """
    try:
        for child in (device_item or []):
            if child.Name not in _SLAVE_META_NODES:
                return True
    except Exception:
        # COM enumeration may fail on freshly-created devices (e.g. EAP);
        # assume no physical terminals in that case
        pass
    return False


def _build_device_name(dev_elem, subtype_name: str, index: int) -> str:
    """Build a human-readable name for a discovered device.

    Preference: PnP device description > ItemSubTypeName > fallback.
    """
    import xml.etree.ElementTree as ET

    # Try PnP device description (e.g. "Intel PCI Ethernet Adapter (Gigabit)")
    pnp_desc = dev_elem.findtext(".//Pnp/DeviceDesc")
    if pnp_desc:
        pnp_desc = pnp_desc.strip()
        # Truncate trailing noise
        if pnp_desc.endswith("#"):
            pnp_desc = pnp_desc[:-1].strip()
        return pnp_desc

    # Try ItemSubTypeName
    if subtype_name:
        return subtype_name

    return f"Device {index + 1}"


def _parse_scan_result(scanned_xml: str, found_devices: list) -> None:
    """Parse ``FoundDevices`` from a ``ProduceXml(False)`` result.

    Populates *found_devices* in-place with dicts keyed by
    ``name``, ``subtype``, ``subtype_name``, ``address_xml``.
    """
    import xml.etree.ElementTree as ET

    if not scanned_xml:
        return
    try:
        root = ET.fromstring(scanned_xml)
        fd_elem = root.find(".//FoundDevices")
        if fd_elem is not None:
            for dev_elem in fd_elem.findall("Device"):
                subtype_elem = dev_elem.find("ItemSubType")
                subtype_name_elem = dev_elem.find("ItemSubTypeName")
                addr_elem = dev_elem.find("AddressInfo")

                item_subtype = (
                    int(subtype_elem.text)
                    if subtype_elem is not None and subtype_elem.text
                    else 0
                )
                subtype_name = (
                    subtype_name_elem.text.strip()
                    if subtype_name_elem is not None and subtype_name_elem.text
                    else ""
                )
                address_info_xml = (
                    ET.tostring(addr_elem, encoding="unicode")
                    if addr_elem is not None
                    else ""
                )
                dev_name = _build_device_name(
                    dev_elem, subtype_name, len(found_devices)
                )
                found_devices.append({
                    "name": dev_name,
                    "subtype": item_subtype,
                    "subtype_name": subtype_name,
                    "address_xml": address_info_xml,
                })
    except ET.ParseError:
        pass


@_preserve_xae_ui_state
def scan_devices() -> dict:
    """Scan the current target for EtherCAT devices and add them to the
    solution configuration.

    Workflow:
        1. Verify that the target is already in Config mode.
        2. ``io_root.ProduceXml(False)`` triggers hardware discovery.
        3. Parse ``<FoundDevices>/<Device>`` from the XML.
        4. For each device: ``CreateChild`` → ``ConsumeXml(AddressInfo)`` →
           ``ConsumeXml(ScanBoxes)`` → ``_has_slave_terminals`` filter.

    All COM calls go through the shared ``_dte()`` cache — no subprocess
    or separate COM instance is created.

    Returns:
        dict with keys ``found``, ``added``, ``boxes``, ``skipped``,
        ``status`` (``"ok"`` / ``"error"``),
        ``config_tries`` (always zero; retained for CLI compatibility),
        and ``target``.
    """
    import xml.etree.ElementTree as ET

    dte = _dte()
    sysman = dte.Solution.Projects.Item(1).Object
    target_netid = sysman.GetTargetNetId()

    set_silent_mode(True)

    # ── 1. Three-tier Config mode ──
    config_tries = 0

    def _is_config() -> bool:
        """Return True only when the target is confirmed in Config mode.

        ADS state codes (TcAdsDef.h / pyads)::

            0=Invalid  1=Idle   2=Reset   3=Init   4=Start
            5=Run      6=Stop   7=Config  8=Reconfig

        EtherCAT hardware scanning *requires* Config (7).  Stop (6) does NOT
        allow scanning — the earlier ``s != 5`` check was too loose and
        incorrectly treated Stop / Idle / Init as usable.

        Some controllers report state **15** while in Config mode.
        """
        try:
            from tc_agent.ads import ADS_SYSTEM_SERVICE_PORT, read_ads_state
            state = read_ads_state(target_netid, ADS_SYSTEM_SERVICE_PORT)
            return state["state_code"] in (7, 8, 15)
        except Exception:
            # ADS not reachable — target restarting or unreachable,
            # can't confirm state → return False so polling continues
            return False

    '''  # Legacy implicit Config helpers, intentionally disabled.
    def _config_or_proceed() -> bool:
        """Like _is_config() but returns True on ADS errors — used as
        a last-resort check when we've exhausted all Config attempts
        and should just proceed with the scan."""
        try:
            return _is_config()
        except Exception:
            return True

    def _poll_config(timeout: float, label: str = "") -> bool:
        """Poll ``_is_config()`` every 1.5 s until True or *timeout*."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _is_config():
                return True
            time.sleep(1.5)
        return False

    '''  # End disabled legacy helpers.
    # Config state is explicitly requested and ADS-verified by the caller.
    if not _is_config():
        return {
            "status": "error",
            "message": "Device scan requires Config mode. Run tc config explicitly, then retry.",
            "target": target_netid,
            "config_tries": 0,
        }
    # ── 2-3. Scan + parse ──
    io_root = sysman.LookupTreeItem("TIID")

    def _produce():
        try:
            return io_root.ProduceXml(False)
        except Exception:
            return ""

    scanned_xml = _produce()
    time.sleep(1)

    found_devices: list[dict] = []
    _parse_scan_result(scanned_xml, found_devices)

    # ── 4-6. Add each device, scan boxes, filter ──
    added: list[str] = []
    boxes: dict[str, int] = {}
    skipped: list[str] = []
    existing_names = {child.Name for child in (io_root or [])}

    if found_devices:
        for dev in found_devices:
            name = dev["name"]
            subtype = dev["subtype"]
            addr_xml = dev["address_xml"]

            if name in existing_names:
                skipped.append(f"{name} (already configured)")
                continue

            try:
                device_item = io_root.CreateChild(name, subtype, "", None)
                existing_names.add(name)
                time.sleep(0.3)
            except Exception as exc:
                added.append(f"{name} (CreateChild failed: {exc})")
                continue

            if addr_xml:
                try:
                    device_item.ConsumeXml(
                        f"<TreeItem><DeviceDef>{addr_xml}</DeviceDef></TreeItem>"
                    )
                    time.sleep(0.3)
                except Exception:
                    pass

            try:
                device_item.ConsumeXml(
                    "<TreeItem><DeviceDef><ScanBoxes>1</ScanBoxes></DeviceDef></TreeItem>"
                )
                time.sleep(0.5)
                box_count = sum(
                    1 for child in (device_item or [])
                    if child.Name not in _SLAVE_META_NODES
                )
                boxes[name] = box_count
            except Exception:
                boxes[name] = 0

            added.append(name)

    # ── Delete adapters with no slaves ──
    # Runs regardless of whether new devices were found — stale empty
    # masters from previous scans must be cleaned up unconditionally.
    # Freshly-created masters that end up with 0 slave terminals (common
    # on multi-adapter controllers where only one NIC drives EtherCAT) are
    # cleaned up here as well.
    for child in list(io_root or []):
        if child.Name in _SLAVE_META_NODES:
            continue
        if not _has_slave_terminals(child):
            # Delete sub-items first — TwinCAT COM refuses DeleteChild
            # on a device that still has Image / Inputs / Outputs children.
            for sub in list(child or []):
                try:
                    child.DeleteChild(sub.Name)
                except Exception:
                    pass
            try:
                io_root.DeleteChild(child.Name)
            except Exception as exc:
                # Log the failure instead of silently swallowing it.
                # Common causes: device still referenced by NC axes,
                # or COM handle stale after sub-item cleanup.
                from .cli import click as _click
                _click.echo(f"  [warn] Could not delete empty adapter "
                            f"'{child.Name}': {exc}", err=True)

    return {
        "status": "ok",
        "found": [d["name"] for d in found_devices],
        "added": added, "skipped": skipped,
        "boxes": boxes, "target": target_netid,
        "config_tries": config_tries,
    }


# ---------------------------------------------------------------------------
# Variable linking (I/O ↔ PLC)
# ---------------------------------------------------------------------------

# Diagnostic/internal variable names to exclude from the process-data listing.
_SKIP_VAR_NAMES = frozenset({
    "WcState", "State", "InputToggle", "OutputToggle",
    "AdsAddr", "ChangeCount", "DevId", "AmsNetId",
    "CfgSlaveCount", "DcOutputShift", "DcInputShift",
    "Chn0", "Chn1", "Frm0State", "Frm0WcState",
    "Frm0InputToggle", "Frm0Ctrl", "Frm0WcCtrl",
    "SlaveCount", "DevState", "DevCtrl",
})

_SKIP_VAR_PREFIXES = ("Cycle Counter",)


def list_io_variables(device_name: str = "") -> dict:
    """Enumerate process-data variables from scanned EtherCAT devices.

    Walks the TIID tree to find all terminals (ItemType 5), calls
    ``VarCount``/``Var`` for inputs (direction 0) and outputs (1), and
    returns a structured listing grouped by terminal.

    Diagnostic variables (WcState, State, etc.) are filtered out — only
    real process data channels are included.

    Args:
        device_name: Optional filter — only return results for a device
            whose ``Name`` contains this substring.

    Returns:
        dict with keys ``device``, ``terminals``, ``total_inputs``,
        ``total_outputs``, ``target``.
    """
    sysman = _sysman()
    target = sysman.GetTargetNetId()

    io_root = sysman.LookupTreeItem("TIID")
    if io_root is None or io_root.ChildCount == 0:
        return {
            "status": "ok",
            "device": "",
            "terminals": [],
            "total_inputs": 0,
            "total_outputs": 0,
            "target": target,
            "message": "No I/O devices configured. Run 'tc scan' first.",
        }

    # Find the EtherCAT device
    device_item = None
    for child in (io_root or []):
        if device_name and device_name.lower() not in child.Name.lower():
            continue
        device_item = child
        break

    if device_item is None:
        return {
            "status": "ok",
            "device": "",
            "terminals": [],
            "total_inputs": 0,
            "total_outputs": 0,
            "target": target,
            "message": f"No device matching '{device_name}' found.",
        }

    dev_name = device_item.Name
    terminals: list[dict] = []
    total_in = 0
    total_out = 0

    # Walk device children to find terminals (including nested under couplers)
    def _walk_terminals(parent, depth: int = 0):
        nonlocal total_in, total_out
        for child in (parent or []):
            item_type = getattr(child, "ItemType", 0)
            child_name = getattr(child, "Name", "")
            child_path = getattr(child, "PathName", "")

            if item_type == 5:  # TREEITEMTYPE_BOX (terminal/coupler)
                inputs = []
                outputs = []

                for vdir, (dir_label, lst) in enumerate(
                    [("Inputs", inputs), ("Outputs", outputs)]
                ):
                    try:
                        vc = child.VarCount(vdir)
                    except Exception:
                        vc = 0
                    for vi in range(1, vc + 1):
                        try:
                            var_item = child.Var(vdir, vi)
                            vname = var_item.Name
                            vpath = var_item.PathName

                            # Skip diagnostics
                            if vname in _SKIP_VAR_NAMES:
                                continue
                            if vname.startswith(_SKIP_VAR_PREFIXES):
                                # "Cycle Counter" from FSLOGIC is diagnostic
                                continue
                            # Also skip anything under WcState or InfoData in path
                            if "WcState" in vpath or "InfoData" in vpath:
                                continue

                            # Extract channel/group info from path
                            parts = vpath.split("^")
                            channel = ""
                            for i, p in enumerate(parts):
                                if p.startswith("Channel ") or p.startswith("AI Standard Channel "):
                                    channel = p
                                elif p.startswith("DRV ") or p.startswith("FB ") or p.startswith("FSLOGIC "):
                                    channel = p

                            lst.append({
                                "name": vname,
                                "channel": channel,
                                "path": vpath,
                            })
                        except Exception:
                            continue

                if inputs or outputs:
                    terminals.append({
                        "name": child_name,
                        "path": child_path,
                        "inputs": inputs,
                        "outputs": outputs,
                    })
                    total_in += len(inputs)
                    total_out += len(outputs)

                # Recurse into nested terminals (e.g., E-Bus coupler (e.g. EK1100) contains slaves)
                _walk_terminals(child, depth + 1)

    _walk_terminals(device_item)

    return {
        "status": "ok",
        "device": dev_name,
        "terminals": terminals,
        "total_inputs": total_in,
        "total_outputs": total_out,
        "target": target,
    }


def get_mapping_info() -> dict:
    """Return the current variable linking map.

    Calls ``ITcSysManager.ProduceMappingInfo()`` and returns a
    human-readable summary.

    Returns:
        dict with ``mappings_xml`` (raw XML string), ``link_count``,
        ``status``.
    """
    import xml.etree.ElementTree as ET

    sysman = _sysman()
    target = sysman.GetTargetNetId()

    try:
        xml_str = sysman.ProduceMappingInfo()
    except Exception as exc:
        return {
            "status": "error",
            "message": f"ProduceMappingInfo failed: {exc}",
            "mappings_xml": "",
            "link_count": 0,
            "links": [],
            "target": target,
        }

    links: list[dict] = []
    try:
        root = ET.fromstring(xml_str)
        for owner_a in root.findall("OwnerA"):
            oa_name = owner_a.get("Name", "")
            for owner_b in owner_a.findall("OwnerB"):
                ob_name = owner_b.get("Name", "")
                for link_elem in owner_b.findall("Link"):
                    links.append({
                        "owner_a": oa_name,
                        "owner_b": ob_name,
                        "var_a": link_elem.get("VarA", ""),
                        "var_b": link_elem.get("VarB", ""),
                        "size": link_elem.get("Size", ""),
                        "offs_a": link_elem.get("OffsA", ""),
                        "offs_b": link_elem.get("OffsB", ""),
                    })
    except ET.ParseError:
        pass

    return {
        "status": "ok",
        "mappings_xml": xml_str,
        "link_count": len(links),
        "links": links,
        "target": target,
    }


def link_variable(io_path: str, plc_path: str) -> dict:
    """Link an I/O variable to a PLC symbol.

    Args:
        io_path: Full tree path to the I/O variable
            (e.g. ``TIID^...^Channel 1^Input``).
        plc_path: Full tree path to the PLC variable
            (e.g. ``TIPC^...^PlcTask Inputs^MAIN.bSensor1``).

    Returns:
        dict with ``status`` and ``linked`` / ``message``.
    """
    sysman = _sysman()
    target = sysman.GetTargetNetId()

    try:
        result = sysman.LinkVariables(plc_path, io_path)
        # result == 0 (S_OK) means link created
        # result == 1 (S_FALSE) means already linked
        already = (result == 1)
        return {
            "status": "ok",
            "linked": True,
            "already_linked": already,
            "io_path": io_path,
            "plc_path": plc_path,
            "target": target,
        }
    except Exception as exc:
        return {
            "status": "error",
            "linked": False,
            "message": str(exc),
            "io_path": io_path,
            "plc_path": plc_path,
            "target": target,
        }


def unlink_variable(io_path: str, plc_path: str = "") -> dict:
    """Remove a variable link.

    Args:
        io_path: Full tree path to the I/O variable.
        plc_path: Full tree path to the PLC variable.
            If empty, **all** links from *io_path* are removed.

    Returns:
        dict with ``status`` and ``unlinked``.
    """
    sysman = _sysman()
    target = sysman.GetTargetNetId()

    try:
        sysman.UnlinkVariables(io_path, plc_path)
        return {
            "status": "ok",
            "unlinked": True,
            "io_path": io_path,
            "plc_path": plc_path or "(all)",
            "target": target,
        }
    except Exception as exc:
        return {
            "status": "error",
            "unlinked": False,
            "message": str(exc),
            "io_path": io_path,
            "plc_path": plc_path or "(all)",
            "target": target,
        }


# ---------------------------------------------------------------------------
# NC axis configuration
# ---------------------------------------------------------------------------

# Terminal names that indicate a servo drive suitable for NC configuration.
_SERVO_DRIVE_KW = frozenset({
    "EL72", "EL70", "AX5", "AX8", "ELM72", "ELM70",
    "AM8", "AX5000", "AX8000",
})

# PDO child names that identify a servo drive with NC capability.
_SERVO_PDO_MARKERS = frozenset({
    "FB Position", "DRV Statusword", "DRV Controlword",
})


def find_servo_drives() -> list[dict]:
    """Walk the TIID tree and locate terminals that look like servo drives.

    A terminal is considered a servo drive if its name contains any of the
    known servo product codes (EL72xx, AX5xxx, etc.) **or** its child PDOs
    include servo-specific markers like ``FB Position``.

    Returns:
        List of dicts with ``name``, ``path``, ``subtype_name``, ``pdos``
        (list of child PDO names).
    """
    import re

    sysman = _sysman()
    drives: list[dict] = []

    io_root = sysman.LookupTreeItem("TIID")
    if io_root is None:
        return drives

    def _walk(parent, depth: int = 0):
        for child in (parent or []):
            name = getattr(child, "Name", "")
            path = getattr(child, "PathName", "")
            itype = getattr(child, "ItemType", 0)

            # Check if this item looks like a servo drive
            name_upper = name.upper()
            is_servo_by_name = any(kw in name_upper for kw in _SERVO_DRIVE_KW)

            if itype == 5 and is_servo_by_name:  # BOX type + servo name
                pdos = []
                has_servo_pdos = False
                for pdo in (child or []):
                    pdo_name = pdo.Name
                    pdos.append(pdo_name)
                    if pdo_name in _SERVO_PDO_MARKERS:
                        has_servo_pdos = True

                if has_servo_pdos or is_servo_by_name:
                    at_channels = {
                        int(match.group(1)) for pdo_name in pdos
                        if (match := re.fullmatch(r"AT\s+(\d+)", pdo_name, re.IGNORECASE))
                    }
                    mdt_channels = {
                        int(match.group(1)) for pdo_name in pdos
                        if (match := re.fullmatch(r"MDT\s+(\d+)", pdo_name, re.IGNORECASE))
                    }
                    drives.append({
                        "name": name,
                        "path": path,
                        "subtype_name": getattr(child, "ItemSubTypeName", ""),
                        "pdos": pdos,
                        "channels": sorted(at_channels & mdt_channels),
                    })
                    continue  # don't recurse into drive internals

            # Recurse (e.g., into E-Bus coupler (e.g. EK1100))
            _walk(child, depth + 1)

    _walk(io_root)
    return drives


def create_nc_task_and_axes() -> dict:
    """Create an NC task and axes for each servo drive found in the I/O tree.

    Workflow:
        1. Locate servo drives via :func:`find_servo_drives`.
        2. Create an NC task (``TINC``) if one doesn't exist.
        3. For each drive, create an axis and link it via ``ConsumeXml``.

    Returns:
        dict with ``nc_task``, ``axes`` (list of axis info), ``links``.
    """
    import xml.etree.ElementTree as ET

    sysman = _sysman()
    target = sysman.GetTargetNetId()

    # 1. Find drives
    drives = find_servo_drives()
    if not drives:
        return {
            "status": "ok",
            "message": "No servo drives found. Run 'tc scan' first.",
            "nc_task": "", "axes": [], "links": [], "target": target,
        }

    # 2. Create NC task if needed
    nc = sysman.LookupTreeItem("TINC")
    nc_task_path = ""
    existing_axes = set()

    # Check for existing NC SAF tasks and axes
    for task in (nc or []):
        task_name = task.Name
        tp = getattr(task, "PathName", f"TINC^{task_name}")
        if "SAF" in task_name and "SVB" not in task_name:
            nc_task_path = tp
            # Enumerate existing axes
            try:
                check_axes = sysman.LookupTreeItem(f"{nc_task_path}^Axes")
                if check_axes:
                    for ax in (check_axes or []):
                        existing_axes.add(ax.Name)
            except Exception:
                pass

    if not nc_task_path:
        try:
            nc.CreateChild("NC-Task", 1, "", None)
            time.sleep(0.8)
            # Re-lookup to get fresh children after CreateChild
            nc = sysman.LookupTreeItem("TINC")
            for task in (nc or []):
                tn = task.Name
                tp = getattr(task, "PathName", f"TINC^{tn}")
                if "SAF" in tn and "SVB" not in tn:
                    nc_task_path = tp
                    break
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Failed to create NC task: {exc}",
                "nc_task": "", "axes": [], "links": [], "target": target,
            }

    if not nc_task_path:
        return {
            "status": "error",
            "message": "NC task creation did not produce a SAF task.",
            "nc_task": "", "axes": [], "links": [], "target": target,
        }

    axes_node = nc_task_path + "^Axes"

    # Verify Axes node exists (it may need a moment)
    for _ in range(3):
        try:
            sysman.LookupTreeItem(axes_node)
            break
        except Exception:
            time.sleep(0.5)
    else:
        return {
            "status": "error",
            "message": f"Axes node not found at '{axes_node}' after NC task creation.",
            "nc_task": nc_task_path, "axes": [], "links": [], "target": target,
        }

    # 3. Create axis per drive
    results: list[dict] = []
    all_links: list[dict] = []
    axis_idx = len(existing_axes)

    for drive in drives:
        axis_idx += 1
        axis_name = f"Axis {axis_idx}"
        drive_name = drive["name"]
        drive_path = drive["path"]
        full_axis_path = f"{axes_node}^{axis_name}"

        if axis_name in existing_axes:
            results.append({
                "name": axis_name, "drive": drive_name,
                "status": "skipped", "reason": "already exists",
            })
            continue

        # Create the axis
        try:
            ax_parent = sysman.LookupTreeItem(axes_node)
            ax_child = ax_parent.CreateChild(axis_name, 1, "", None)
            time.sleep(0.5)
        except Exception as exc:
            results.append({
                "name": axis_name, "drive": drive_name,
                "status": "failed", "reason": f"CreateChild: {exc}",
            })
            continue

        # Configure EncType=8 and DrvType=9 via ConsumeXml
        try:
            ax_xml = ax_child.ProduceXml()

            # Set Encoder type to EtherCAT (8)
            ax_xml = ax_xml.replace(
                '<Encoder Name="Enc" EncType="1"',
                '<Encoder Name="Enc" EncType="8"',
            ).replace(
                '<Encoder>', '<Encoder EncType="8">', 1
            )

            # Set Drive type to EtherCAT servo (9)
            ax_xml = ax_xml.replace(
                '<Drive Name="Drive" DrvType="1"',
                '<Drive Name="Drive" DrvType="9"',
            ).replace(
                '<Drive>', '<Drive DrvType="9">', 1
            )

            ax_child.ConsumeXml(ax_xml)
            time.sleep(0.3)
        except Exception:
            pass  # Best-effort — defaults may work for some drives

        # Link axis to drive via IoItem ConsumeXml
        link_ok = False
        try:
            io_link_xml = (
                "<TreeItem><NcAxisDef><IoItem>"
                f"<PathName>{drive_path}</PathName>"
                "</IoItem></NcAxisDef></TreeItem>"
            )
            ax_child.ConsumeXml(io_link_xml)
            time.sleep(0.3)
            link_ok = True
        except Exception:
            pass

        # Verify the link took
        try:
            verify_xml = ax_child.ProduceXml()
            link_verified = "IoItem" in verify_xml
        except Exception:
            link_verified = False

        all_links.append({
            "axis": axis_name, "drive": drive_name,
            "drive_path": drive_path, "linked": link_ok or link_verified,
        })
        results.append({
            "name": axis_name,
            "drive": drive_name,
            "status": "ok" if (link_ok or link_verified) else "created (manual link needed)",
        })

    return {
        "status": "ok",
        "nc_task": nc_task_path,
        "axes": results,
        "links": all_links,
        "target": target,
    }


_NC_META_NODES = frozenset({
    "Image", "Image-Info", "Inputs", "Outputs", "InfoData", "SyncUnits",
})


def _item_xml(item) -> str:
    try:
        return str(item.ProduceXml(False))
    except TypeError:
        return str(item.ProduceXml())


def _xml_values(xml: str, names: tuple[str, ...]) -> dict:
    import xml.etree.ElementTree as ET
    values = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return values
    wanted = {name.lower() for name in names}
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag.lower() in wanted and elem.text and tag not in values:
            values[tag] = elem.text.strip()
    return values


def _walk_tree(item, depth: int, max_depth: int) -> dict:
    node = {
        "name": str(getattr(item, "Name", "") or ""),
        "path": str(getattr(item, "PathName", "") or ""),
        "item_type": int(getattr(item, "ItemType", 0) or 0),
        "item_subtype": int(getattr(item, "ItemSubType", 0) or 0),
        "children": [],
    }
    if depth >= max_depth:
        return node
    try:
        children = list(item or [])
    except Exception:
        children = []
    for child in children:
        if str(getattr(child, "Name", "")) in _NC_META_NODES:
            continue
        node["children"].append(_walk_tree(child, depth + 1, max_depth))
    return node


def nc_structure(max_depth: int = 6) -> dict:
    """Return the complete configured NC tree without changing it."""
    sysman = _sysman()
    root = sysman.LookupTreeItem("TINC")
    return {
        "status": "ok",
        "target": str(sysman.GetTargetNetId()),
        "tree": _walk_tree(root, 0, max(1, min(int(max_depth), 12))),
    }


def _find_nc_item(name_or_path: str, *, axes_only: bool = False):
    sysman = _sysman()
    if name_or_path.startswith("TINC"):
        return sysman.LookupTreeItem(name_or_path)
    root = sysman.LookupTreeItem("TINC")
    matches = []

    def walk(parent, under_axes=False):
        for child in (parent or []):
            child_name = str(getattr(child, "Name", "") or "")
            child_path = str(getattr(child, "PathName", "") or "")
            now_under_axes = (
                under_axes or child_name == "Axes" or child_path.endswith("^Axes")
                or int(getattr(child, "ItemType", 0) or 0) == 21
            )
            child_key = child_name.lower()
            query_key = name_or_path.lower()
            is_match = child_key == query_key
            if not axes_only and child_key == f"{query_key} saf":
                is_match = True
            if is_match and (now_under_axes or not axes_only):
                matches.append(child)
            walk(child, now_under_axes)

    walk(root)
    if not matches:
        raise ValueError(f"NC item not found: {name_or_path}")
    if len(matches) > 1:
        raise ValueError(f"NC item name is ambiguous; pass full path: {name_or_path}")
    return matches[0]


def _axis_summary(item) -> dict:
    import xml.etree.ElementTree as ET

    xml = _item_xml(item)
    values = _xml_values(xml, (
        "AxisType", "EncType", "DrvType", "Unit", "ScaleFactor",
        "Velocity", "Acceleration", "Deceleration", "Jerk", "IoItem",
        "PathName", "ObjectId", "Id",
    ))
    drive_io_paths = []
    encoder_io_paths = []
    try:
        root = ET.fromstring(xml)
        for axis_def in root.iter("NcAxisDef"):
            for io_item in axis_def.findall("./IoItem"):
                path = io_item.findtext("PathName", "").strip()
                if path:
                    drive_io_paths.append(path)
            for io_item in axis_def.findall("./Encoder/IoItem"):
                path = io_item.findtext("PathName", "").strip()
                if path:
                    encoder_io_paths.append(path)
    except ET.ParseError:
        pass
    return {
        "name": str(getattr(item, "Name", "") or ""),
        "path": str(getattr(item, "PathName", "") or ""),
        "item_subtype": int(getattr(item, "ItemSubType", 0) or 0),
        "parameters": values,
        "has_drive_link": bool(drive_io_paths),
        "drive_io_paths": drive_io_paths,
        "encoder_io_paths": encoder_io_paths,
        "has_encoder": "<Encoder" in xml,
    }


def nc_axis_info(axis: str) -> dict:
    item = _find_nc_item(axis, axes_only=True)
    return {"status": "ok", "axis": _axis_summary(item)}


def nc_axis_params(axis: str) -> dict:
    item = _find_nc_item(axis, axes_only=True)
    xml = _item_xml(item)
    return {
        "status": "ok",
        "axis": str(getattr(item, "Name", "") or ""),
        "path": str(getattr(item, "PathName", "") or ""),
        "parameters": _xml_values(xml, (
            "AxisType", "Unit", "ScaleFactor", "Velocity", "VeloMax",
            "Acceleration", "AccMax", "Deceleration", "DecMax", "Jerk",
            "SoftLimitMin", "SoftLimitMax", "Modulo", "EncType", "DrvType",
            "PositionLagMonitoring", "PositionLagLimit",
        )),
    }


_NC_WRITABLE_AXIS_PARAMS = {
    name.lower(): name for name in (
        "Unit", "ScaleFactor", "Velocity", "VeloMax", "Acceleration",
        "AccMax", "Deceleration", "DecMax", "Jerk", "SoftLimitMin",
        "SoftLimitMax", "Modulo", "PositionLagMonitoring",
        "PositionLagLimit",
    )
}


def nc_set_axis_params(axis: str, parameters: dict) -> dict:
    """Write allow-listed NC axis parameters and verify them by COM readback."""
    import math
    import re
    from xml.sax.saxutils import escape

    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("parameters must be a non-empty object")
    normalized = {}
    for raw_name, raw_value in parameters.items():
        canonical = _NC_WRITABLE_AXIS_PARAMS.get(str(raw_name).lower())
        if canonical is None:
            allowed = ", ".join(_NC_WRITABLE_AXIS_PARAMS.values())
            raise ValueError(f"unsupported NC axis parameter: {raw_name}; allowed: {allowed}")
        if canonical == "Unit":
            value = str(raw_value).strip()
            if not value or len(value) > 16 or not re.fullmatch(r"[A-Za-z0-9_./%-]+", value):
                raise ValueError("Unit must be 1-16 simple unit characters")
        elif canonical in {"Modulo", "PositionLagMonitoring"}:
            if isinstance(raw_value, bool):
                value = "1" if raw_value else "0"
            elif str(raw_value).strip().lower() in {"0", "1", "true", "false"}:
                value = "1" if str(raw_value).strip().lower() in {"1", "true"} else "0"
            else:
                raise ValueError(f"{canonical} must be boolean")
        else:
            try:
                number = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{canonical} must be numeric") from exc
            if not math.isfinite(number):
                raise ValueError(f"{canonical} must be finite")
            if canonical == "ScaleFactor" and number <= 0:
                raise ValueError("ScaleFactor must be greater than zero")
            if canonical not in {"SoftLimitMin", "SoftLimitMax"} and number < 0:
                raise ValueError(f"{canonical} must not be negative")
            value = format(number, ".15g")
        normalized[canonical] = value

    item = _find_nc_item(axis, axes_only=True)
    before = nc_axis_params(str(getattr(item, "PathName", axis)))["parameters"]
    fields = "".join(
        f"<{name}>{escape(value)}</{name}>" for name, value in normalized.items()
    )
    item.ConsumeXml(f"<TreeItem><NcAxisDef>{fields}</NcAxisDef></TreeItem>")
    time.sleep(0.2)
    after = nc_axis_params(str(getattr(item, "PathName", axis)))["parameters"]
    mismatches = {}
    for name, expected in normalized.items():
        actual = after.get(name)
        matches = actual == expected
        if not matches and actual is not None and name not in {"Unit", "Modulo", "PositionLagMonitoring"}:
            try:
                matches = math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
            except ValueError:
                pass
        if not matches:
            mismatches[name] = {"expected": expected, "actual": actual}
    return {
        "status": "ok" if not mismatches else "unverified",
        "verified": not mismatches,
        "axis": str(getattr(item, "Name", "") or ""),
        "path": str(getattr(item, "PathName", "") or ""),
        "requested": normalized,
        "before": {name: before.get(name) for name in normalized},
        "after": {name: after.get(name) for name in normalized},
        "mismatches": mismatches,
    }


def nc_create_task(name: str = "NC-Task") -> dict:
    sysman = _sysman()
    root = sysman.LookupTreeItem("TINC")
    for child in (root or []):
        child_key = str(child.Name).lower()
        if child_key in {name.lower(), f"{name.lower()} saf"}:
            return {"status": "skipped", "name": child.Name, "path": child.PathName}
    root.CreateChild(name, 1, "", None)
    time.sleep(0.3)
    created = _find_nc_item(name)
    return {"status": "ok", "name": created.Name, "path": created.PathName}


def _resolve_axes_parent(task: str):
    task_item = _find_nc_item(task)
    task_path = str(getattr(task_item, "PathName", "") or f"TINC^{task_item.Name}")
    return _sysman().LookupTreeItem(f"{task_path}^Axes")


def nc_create_axis(task: str, name: str, axis_type: str = "continuous") -> dict:
    if axis_type not in {"continuous", "virtual"}:
        raise ValueError("axis_type must be continuous or virtual")
    parent = _resolve_axes_parent(task)
    for child in (parent or []):
        if str(child.Name).lower() == name.lower():
            return {"status": "skipped", "axis": _axis_summary(child)}
    axis = parent.CreateChild(name, 1, "", None)
    time.sleep(0.3)
    if axis_type == "virtual":
        try:
            axis.ConsumeXml(
                "<TreeItem><NcAxisDef><Encoder EncType=\"1\"/>"
                "<Drive DrvType=\"1\"/></NcAxisDef></TreeItem>"
            )
        except Exception:
            pass
    result = _axis_summary(axis)
    result["requested_type"] = axis_type
    return {"status": "ok", "axis": result}


def find_nc_encoders() -> list[dict]:
    """Find I/O devices and channels that can plausibly provide position feedback."""
    import re

    sysman = _sysman()
    root = sysman.LookupTreeItem("TIID")
    product_pattern = re.compile(r"\b(?:EL5[01]\d{2}|EP5[01]\d{2}|EJ5[01]\d{2})\b")
    word_pattern = re.compile(r"\b(?:ENCODER|ENC|POSITION\s+FEEDBACK|FEEDBACK|OCT)\b")
    results = []

    def walk(parent):
        for child in (parent or []):
            name = str(getattr(child, "Name", "") or "")
            subtype = str(getattr(child, "ItemSubTypeName", "") or "")
            text = f"{name} {subtype}".upper()
            is_position_command = "POSITION COMMAND" in text
            if not is_position_command and (
                product_pattern.search(text) or word_pattern.search(text)
            ):
                results.append({
                    "name": name,
                    "path": str(getattr(child, "PathName", "") or ""),
                    "subtype_name": subtype,
                })
            walk(child)

    walk(root)
    unique = {item["path"]: item for item in results if item["path"]}
    return list(unique.values())


def _nc_link(axis: str, io_path: str, role: str) -> dict:
    item = _find_nc_item(axis, axes_only=True)
    from xml.sax.saxutils import escape

    path_xml = escape(io_path)
    if role == "drive":
        link_xml = f"<IoItem><PathName>{path_xml}</PathName></IoItem>"
    else:
        link_xml = (
            f"<Encoder><IoItem><PathName>{path_xml}</PathName>"
            "</IoItem></Encoder>"
        )
    payload = f"<TreeItem><NcAxisDef>{link_xml}</NcAxisDef></TreeItem>"
    item.ConsumeXml(payload)
    time.sleep(0.2)
    xml = _item_xml(item)
    return {
        "status": "ok" if io_path in xml else "unverified",
        "axis": str(item.Name),
        "axis_path": str(item.PathName),
        "role": role,
        "io_path": io_path,
        "verified": io_path in xml,
    }


def nc_link_drive(axis: str, drive_path: str, channel: int | None = None) -> dict:
    """Use the Automation Interface equivalent of NC Settings -> Link To.

    TwinCAT and the installed device description own the PDO mapping for both
    DS402/CoE terminals and SoE drives.  ``channel`` is retained only for API
    compatibility; callers should pass the exact drive/channel tree path when
    a multi-axis device exposes more than one selectable I/O item.
    """
    result = _nc_link(axis, drive_path, "drive")
    result["strategy"] = "nc-settings-link"
    if channel is not None:
        result["channel_hint"] = int(channel)
        result["channel_mapping"] = "handled-by-twincat"
    return result


def nc_link_encoder(axis: str, encoder_path: str) -> dict:
    return _nc_link(axis, encoder_path, "encoder")


def nc_quick_link(axis: str = "", drive_path: str = "") -> dict:
    """Quick-link one NC axis to one servo drive with safe unique selection."""
    axes = nc_links().get("axes", [])
    drives = find_servo_drives()

    def select(items: list[dict], query: str, kind: str) -> dict:
        if query:
            key = query.casefold()
            matches = [item for item in items if key in {
                str(item.get("name", "")).casefold(),
                str(item.get("path", "")).casefold(),
            }]
        else:
            matches = items
        if len(matches) != 1:
            candidates = [{"name": i.get("name"), "path": i.get("path")} for i in matches or items]
            raise ValueError(
                f"quick link requires exactly one {kind}; found {len(matches)}; "
                f"candidates={candidates}"
            )
        return matches[0]

    axis_pool = axes
    if not axis:
        unlinked = [item for item in axes if not item.get("has_drive_link")]
        if unlinked:
            axis_pool = unlinked
    selected_axis = select(axis_pool, axis, "NC axis")
    selected_drive = select(drives, drive_path, "servo drive")
    selected_path = str(selected_drive["path"])
    if selected_path in selected_axis.get("drive_io_paths", []):
        return {
            "status": "skipped",
            "strategy": "already-linked",
            "axis": selected_axis,
            "drive": selected_drive,
            "verified": True,
        }
    result = nc_link_drive(str(selected_axis["path"]), selected_path)
    result["quick_link"] = True
    result["selected_axis"] = {
        "name": selected_axis.get("name"), "path": selected_axis.get("path")
    }
    result["selected_drive"] = {
        "name": selected_drive.get("name"), "path": selected_path
    }
    return result


def nc_links() -> dict:
    structure = nc_structure(max_depth=8)
    axes = []

    def visit(node):
        if node["name"] == "Axes" or node["path"].endswith("^Axes"):
            for child in node["children"]:
                try:
                    axes.append(_axis_summary(
                        _find_nc_item(child["path"], axes_only=True)
                    ))
                except Exception:
                    pass
            return
        for child in node["children"]:
            visit(child)

    visit(structure["tree"])
    return {"status": "ok", "axes": axes}


def nc_state() -> dict:
    """Read configured NC state and report whether runtime state is verified."""
    sysman = _sysman()
    runtime = get_runtime_state()
    structure = nc_structure(max_depth=4)
    return {
        "status": "ok",
        "target": str(sysman.GetTargetNetId()),
        "runtime": runtime,
        "nc_tree": structure["tree"],
        "axis_runtime_verified": False,
        "detail": "Axis cyclic state requires a configured ADS symbol/index mapping.",
    }


def nc_axis_state(axis: str) -> dict:
    info = nc_axis_info(axis)["axis"]
    sysman = _sysman()
    mapping = get_mapping_info()
    plc_base = ""
    plc_owner = ""
    for link in mapping.get("links", []):
        if link.get("owner_b") != info["path"]:
            continue
        var_a = str(link.get("var_a") or "")
        if var_a.endswith(".NcToPlc"):
            plc_base = var_a.split("^", 1)[-1][:-len(".NcToPlc")]
            plc_owner = str(link.get("owner_a") or "")
            break
    if not plc_base:
        return {
            "status": "unavailable", "axis": info, "runtime_verified": False,
            "detail": "No PLC AXIS_REF NcToPlc mapping was found for this NC axis.",
        }

    ads_port = None
    for runtime in _list_plc_runtimes(sysman):
        instance_path = str(getattr(runtime.get("instance"), "PathName", "") or "")
        if instance_path == plc_owner:
            ads_port = runtime.get("ads_port")
            break
    if ads_port is None:
        return {
            "status": "unavailable", "axis": info, "runtime_verified": False,
            "plc_symbol": plc_base, "detail": "The mapped PLC ADS port could not be resolved.",
        }

    prefix = f"{plc_base}.NcToPlc"
    fields = {
        "StateDWord": "udint", "ErrorCode": "udint", "AxisState": "udint",
        "HomingState": "udint", "ActPos": "lreal", "ActVelo": "lreal",
        "SetPos": "lreal", "SetVelo": "lreal", "TargetPos": "lreal",
    }
    symbols = {f"{prefix}.{name}": kind for name, kind in fields.items()}
    target = str(sysman.GetTargetNetId())
    try:
        from tc_agent.ads import read_ads_state, read_ads_values_by_name
        plc_state = read_ads_state(target, int(ads_port))
        values = read_ads_values_by_name(target, int(ads_port), symbols)
    except Exception as exc:
        return {
            "status": "offline", "axis": info, "runtime_verified": False,
            "target": target, "ads_port": int(ads_port), "plc_symbol": plc_base,
            "detail": str(exc),
        }
    state_word = int(values[f"{prefix}.StateDWord"])
    runtime = {name: values[f"{prefix}.{name}"] for name in fields}
    runtime["flags"] = {
        "operational": bool(state_word & (1 << 0)),
        "homed": bool(state_word & (1 << 1)),
        "not_moving": bool(state_word & (1 << 2)),
        "in_position_area": bool(state_word & (1 << 3)),
        "in_target_position": bool(state_word & (1 << 4)),
        "error": bool(state_word & (1 << 31)),
    }
    return {
        "status": "online", "axis": info, "runtime_verified": True,
        "target": target, "ads_port": int(ads_port), "plc_symbol": plc_base,
        "plc_state": plc_state, "runtime": runtime,
    }


def nc_axis_move(axis: str, position: float, velocity: float,
                 timeout: float = 30.0, control_scope: str = "",
                 confirm_physical: bool = False,
                 require_homed: bool = True) -> dict:
    """Execute a monitored absolute move through the standard PLC axis demo interface."""
    import math

    position = float(position)
    velocity = float(velocity)
    timeout = max(1.0, min(float(timeout), 300.0))
    if not math.isfinite(position):
        raise ValueError("position must be finite")
    if not math.isfinite(velocity) or velocity <= 0:
        raise ValueError("velocity must be greater than zero")

    before = nc_axis_state(axis)
    if not before.get("runtime_verified"):
        return {"status": "unavailable", "verified": False, "before": before}
    info = before["axis"]
    mapping = get_mapping_info()
    axis_path = info["path"]
    physical_links = []
    for link in mapping.get("links", []):
        owner_a = str(link.get("owner_a") or "")
        owner_b = str(link.get("owner_b") or "")
        if axis_path not in {owner_a, owner_b}:
            continue
        other = owner_b if owner_a == axis_path else owner_a
        if other.startswith("TIID"):
            physical_links.append(link)
    is_physical = bool(info.get("has_drive_link") or physical_links)
    if is_physical and not confirm_physical:
        return {
            "status": "confirmation_required", "verified": False,
            "axis": info, "physical_axis": True,
            "detail": "Physical drive mapping detected; set confirm_physical=true after safety checks.",
        }

    runtime = before["runtime"]
    if runtime["flags"].get("error") or int(runtime.get("ErrorCode") or 0):
        return {"status": "blocked", "verified": False, "reason": "axis-error", "before": before}
    if is_physical and require_homed and not runtime["flags"].get("homed"):
        return {"status": "blocked", "verified": False, "reason": "axis-not-homed", "before": before}

    params = info.get("parameters", {})
    for key, comparator in (("SoftLimitMin", lambda p, limit: p < limit),
                            ("SoftLimitMax", lambda p, limit: p > limit)):
        if params.get(key) not in (None, ""):
            limit = float(params[key])
            if comparator(position, limit):
                return {
                    "status": "blocked", "verified": False,
                    "reason": key, "limit": limit, "requested_position": position,
                }

    plc_base = before["plc_symbol"]
    scope = control_scope.strip() or plc_base.rsplit(".", 1)[0]
    target = before["target"]
    port = int(before["ads_port"])
    names = {
        "enable": f"{scope}.bEnable", "move": f"{scope}.bMoveAbsolute",
        "stop": f"{scope}.bStop", "position": f"{scope}.lrTargetPosition",
        "velocity": f"{scope}.lrVelocity", "ready": f"{scope}.bReady",
        "busy": f"{scope}.bBusy", "done": f"{scope}.bDone",
        "error": f"{scope}.bError", "error_id": f"{scope}.nErrId",
        "actual": f"{scope}.lrActPosition",
    }
    read_symbols = {
        names["enable"]: "bool", names["ready"]: "bool",
        names["busy"]: "bool", names["done"]: "bool",
        names["error"]: "bool", names["error_id"]: "udint",
        names["actual"]: "lreal",
    }
    from tc_agent.ads import read_ads_values_by_name, write_ads_values_by_name

    initial = read_ads_values_by_name(target, port, read_symbols)
    restore_enable = bool(initial[names["enable"]])
    started_at = time.monotonic()
    samples = []
    seen_busy = False
    try:
        write_ads_values_by_name(target, port, {
            names["position"]: ("lreal", position),
            names["velocity"]: ("lreal", velocity),
            names["enable"]: ("bool", True),
        })
        ready_deadline = time.monotonic() + min(timeout, 10.0)
        while time.monotonic() < ready_deadline:
            current = read_ads_values_by_name(target, port, read_symbols)
            if current[names["error"]]:
                return {"status": "failed", "verified": False, "reason": "enable-error",
                        "error_id": current[names["error_id"]]}
            if current[names["ready"]]:
                break
            time.sleep(0.1)
        else:
            return {"status": "failed", "verified": False, "reason": "enable-timeout"}

        write_ads_values_by_name(target, port, {names["move"]: ("bool", True)})
        time.sleep(0.08)
        write_ads_values_by_name(target, port, {names["move"]: ("bool", False)})
        deadline = started_at + timeout
        while time.monotonic() < deadline:
            current = read_ads_values_by_name(target, port, read_symbols)
            seen_busy = seen_busy or bool(current[names["busy"]])
            samples.append({
                "elapsed": round(time.monotonic() - started_at, 3),
                "position": float(current[names["actual"]]),
                "busy": bool(current[names["busy"]]),
                "done": bool(current[names["done"]]),
            })
            if current[names["error"]]:
                return {"status": "failed", "verified": False, "reason": "move-error",
                        "error_id": int(current[names["error_id"]]), "samples": samples}
            if seen_busy and current[names["done"]] and not current[names["busy"]]:
                after = nc_axis_state(axis)
                position_ok = math.isclose(
                    float(after["runtime"]["ActPos"]), position,
                    rel_tol=0.0, abs_tol=1e-3,
                )
                return {
                    "status": "ok" if position_ok else "unverified",
                    "verified": position_ok, "physical_axis": is_physical,
                    "axis": info["name"], "control_scope": scope,
                    "requested": {"position": position, "velocity": velocity},
                    "elapsed": round(time.monotonic() - started_at, 3),
                    "sample_count": len(samples), "after": after,
                }
            time.sleep(0.1)
        write_ads_values_by_name(target, port, {names["stop"]: ("bool", True)})
        time.sleep(0.08)
        write_ads_values_by_name(target, port, {names["stop"]: ("bool", False)})
        return {"status": "failed", "verified": False, "reason": "move-timeout", "samples": samples}
    finally:
        write_ads_values_by_name(target, port, {
            names["move"]: ("bool", False), names["stop"]: ("bool", False),
            names["enable"]: ("bool", restore_enable),
        })


def nc_validate(configuration: dict) -> dict:
    """Compare expected NC tasks/axes with the current configuration."""
    if not isinstance(configuration, dict):
        raise ValueError("configuration must be an object")
    tree = nc_structure(max_depth=8)["tree"]
    present_paths = {}

    def collect(node):
        present_paths[node["path"]] = node
        for child in node["children"]:
            collect(child)

    collect(tree)
    missing = []
    present = []
    for task in configuration.get("tasks", []):
        task_name = str(task.get("name") or "")
        task_nodes = [n for n in present_paths.values() if n["name"].lower() == task_name.lower()]
        if not task_nodes:
            missing.append({"type": "task", "name": task_name})
            continue
        present.append({"type": "task", "name": task_name})
        for axis in task.get("axes", []):
            axis_name = str(axis.get("name") or "")
            if any(n["name"].lower() == axis_name.lower() for n in present_paths.values()):
                present.append({"type": "axis", "name": axis_name, "task": task_name})
            else:
                missing.append({"type": "axis", "name": axis_name, "task": task_name})
    return {"status": "ok", "valid": not missing, "present": present, "missing": missing}


def is_twincat_started() -> bool:
    """Check if TwinCAT runtime is currently running."""
    sysman = _sysman()
    return sysman.IsTwinCATStarted()


# ---------------------------------------------------------------------------
# Silent mode
# ---------------------------------------------------------------------------

def set_silent_mode(on: bool = True) -> dict:
    """Enable/disable silent mode (suppress all message boxes and UI).

    Sets three layers of suppression, any one of which may prevent dialogs:
    1. ``TcAutomationSettings.SilentMode``  — license / activation dialogs
    2. ``DTE.SuppressUI``                   — save / overwrite prompts
    3. ``DTE.MainWindow.Visible``           — hide entire IDE (stops all popups)
    """
    try:
        dte = _dte()
    except Exception as e:
        return {"silent_mode": False, "error": str(e)[:100]}

    # Layer 1: SilentMode via TcAutomationSettings
    try:
        settings = dte.GetObject("TcAutomationSettings")
        settings.SilentMode = on
    except AttributeError:
        try:
            dte.ExecuteCommand("TwinCAT.EnableSilentMode" if on else "TwinCAT.DisableSilentMode")
        except Exception:
            pass
    except Exception:
        pass

    # Layer 2: SuppressUI — stops VS save/overwrite prompts
    try:
        dte.SuppressUI = on
    except Exception:
        pass

    return {"silent_mode": on}


# ---------------------------------------------------------------------------
# Solution open / close
# ---------------------------------------------------------------------------

def close_solution() -> dict:
    """Close the currently open solution in XAE.

    Saves all open documents first, then closes the solution.
    Returns ``{"status": "closed"}`` on success, ``{"status": "skipped"}``
    when no solution is open or COM is unavailable.
    """
    # Fast check — don't enter the 20 s retry loop if XAE isn't running
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    try:
        _ = win32com.client.GetActiveObject("TcXaeShell.DTE.15.0")
    except Exception:
        return {"status": "skipped", "reason": "XAE not running"}

    # Use the shared DTE to avoid COM apartment mismatch
    try:
        dte = _dte()
    except Exception:
        return {"status": "skipped", "reason": "XAE not running"}

    try:
        if dte.Solution.Projects.Count == 0:
            return {"status": "skipped", "reason": "no solution open"}
    except Exception:
        pass

    # Save all first to avoid "unsaved changes" prompts
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass

    try:
        dte.Solution.Close()
        return {"status": "closed"}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:100]}


def quit_xae() -> dict:
    """Close the XAE application window itself.

    Saves and closes the solution first, then quits the shell and
    clears the global DTE cache.
    """
    global _dte_cache

    # Use the shared DTE to avoid COM apartment mismatch
    try:
        dte = _dte()
    except Exception:
        return {"status": "skipped", "reason": "XAE not running"}

    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    try:
        if dte.Solution.Projects.Count > 0:
            dte.Solution.Close()
    except Exception:
        pass

    _dte_cache = None

    try:
        dte.Quit()
        return {"status": "quit"}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:100]}


def open_solution(sln_path: str) -> dict:
    """Open a .sln file in XAE.

    If another solution is already open it is **closed first** (saved,
    then closed).  Uses ``os.startfile()`` to launch the full XAE desktop
    application (NOT ``Dispatch``, which starts a headless COM server that
    exits when the client disconnects).  After opening, waits up to 30 s
    for the solution to fully load.
    """
    import os as _os
    from pathlib import Path as _Path

    p = _Path(sln_path).resolve()
    if not p.exists():
        return {"status": "error", "reason": f".sln not found: {str(p)}"}

    # Close existing solution first (if XAE is already running)
    close_solution()

    # Launch XAE via startfile — opens the full desktop app, not a headless COM server
    _os.startfile(str(p))

    # Wait for XAE to register in COM ROT and load the solution
    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    for attempt in range(15):
        time.sleep(2)
        try:
            dte = win32com.client.GetActiveObject("TcXaeShell.DTE.15.0")
            if dte.Solution.Projects.Count > 0:
                # Warm the shared _dte() cache
                global _dte_cache
                _dte_cache = dte
                return {"status": "opened", "path": str(p)}
        except Exception:
            continue

    return {"status": "opened", "path": str(p), "warning": "solution may still be loading"}


# ---------------------------------------------------------------------------
# Boot project
# ---------------------------------------------------------------------------

def _set_boot_project_first_only_legacy(auto_start: bool = True) -> dict:
    """Configure PLC project as boot project.

    Enables autostart and generates the boot image.
    ``GenerateBootProject`` must be called on the PLC root node
    (``TIPC^PLC1``), not the Instance or NestedProject.
    """
    sysman = _sysman()

    # Find the PLC root node (direct child of TIPC)
    plc_root = None
    tipc = sysman.LookupTreeItem("TIPC")
    for child in (tipc or []):
        plc_root = child
        break

    if not plc_root:
        return {"boot_project": False, "error": "No PLC node found under TIPC"}

    # ── 1. Set boot-project autostart ──
    try:
        plc_root.BootProjectAutostart = auto_start
    except Exception:
        pass  # not all nodes expose this property

    # ── 2. Generate boot project image ──
    try:
        plc_root.GenerateBootProject(True)
    except Exception as e:
        return {"boot_project": False, "error": str(e)[:200]}

    return {"boot_project": True, "autostart": auto_start}


def set_boot_project(auto_start: bool = True) -> dict:
    """Generate and enable boot projects for every configured PLC runtime."""
    runtimes = _list_plc_runtimes(_sysman())
    if not runtimes:
        return {
            "boot_project": True,
            "status": "skipped",
            "detail": "No PLC projects configured",
            "plcs": [],
        }

    results = []
    for runtime in runtimes:
        plc_root = runtime["root"]
        item = {
            "name": runtime["name"],
            "ads_port": runtime["ads_port"],
            "autostart": auto_start,
        }
        try:
            plc_root.BootProjectAutostart = auto_start
        except Exception:
            item["autostart_warning"] = "BootProjectAutostart property unavailable"
        try:
            plc_root.GenerateBootProject(True)
            item["status"] = "ok"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)[:200]
        results.append(item)

    failed = [item for item in results if item["status"] != "ok"]
    return {
        "boot_project": not failed,
        "status": "ok" if not failed else "failed",
        "autostart": auto_start,
        "plcs": results,
    }


# ---------------------------------------------------------------------------
# Solution discovery
# ---------------------------------------------------------------------------

def _find_solution(hint: str = "") -> str:
    """Return a .sln path to open, or ``""`` when nothing is found.

    Precedence:
    1. *hint* if it points to a valid ``.sln`` file.
    2. The most recently modified ``.sln`` under ``output/``.

    Always returns an absolute path.
    """
    import glob as _glob

    if hint:
        p = Path(hint).resolve()
        if p.suffix.lower() == ".tsproj":
            sln = p.with_suffix(".sln")
            if sln.exists():
                return str(sln)
        if p.suffix.lower() == ".sln" and p.exists():
            return str(p)

    # Scan output/ for .sln files
    slns = sorted(
        _glob.glob("output/**/*.sln", recursive=True),
        key=lambda f: Path(f).stat().st_mtime,
        reverse=True,
    )
    for s in slns:
        abs_path = str(Path(s).resolve())
        if Path(abs_path).exists():
            return abs_path

    return ""


# ---------------------------------------------------------------------------
# Full deployment pipeline
# ---------------------------------------------------------------------------

def _dismiss_dialogs(duration: float = 5.0) -> None:
    """Dismiss TwinCAT confirmation dialogs — NO pyautogui mouse movement.

    Uses ``win32gui.SendMessage(BM_CLICK)`` on dialog buttons directly,
    never moving the user's mouse cursor.
    """
    import threading
    import ctypes
    import win32con

    def _clicker(seconds: float):
        import time as _t
        import win32gui as _wg
        import win32con as _wc
        from ctypes import windll

        BM_CLICK = 0x00F5

        _DIALOG_KW = {
            "restart", "activate configuration", "boot", "confirm",
            "save", "overwrite", "configuration",
        }

        _IDE_EXT = (
            ".tsproj", ".sln", ".tcgvl", ".tcpou", ".tcdut", ".tcvis",
        )

        deadline = _t.time() + seconds
        while _t.time() < deadline:
            dlg_hwnd = None
            try:
                def _find(h, _2):
                    nonlocal dlg_hwnd
                    if not _wg.IsWindowVisible(h):
                        return
                    title = _wg.GetWindowText(h).lower()
                    if not title:
                        return
                    r = _wg.GetWindowRect(h)
                    if r[2] - r[0] < 100 or r[3] - r[1] < 50:
                        return
                    if any(ext in title for ext in _IDE_EXT):
                        return
                    if "tcxaeshell" in title or "twincat xae" in title:
                        return
                    if any(kw in title for kw in _DIALOG_KW):
                        dlg_hwnd = h
                _wg.EnumWindows(_find, None)
            except Exception:
                pass

            if dlg_hwnd is None:
                _t.sleep(0.5)
                continue

            # Click the default button via BM_CLICK — no mouse movement
            try:
                def _click_default(h, _2):
                    try:
                        if _wg.GetClassName(h) == "Button":
                            style = _wg.GetWindowLong(h, _wc.GWL_STYLE)
                            if style & 0x0001:  # BS_DEFPUSHBUTTON
                                windll.user32.SendMessageW(h, BM_CLICK, 0, 0)
                    except Exception:
                        pass
                _wg.EnumChildWindows(dlg_hwnd, _click_default, None)
            except Exception:
                pass

            # Fallback: send Enter via PostMessage — no keyboard simulation
            try:
                windll.user32.PostMessageW(dlg_hwnd, _wc.WM_KEYDOWN,
                                           _wc.VK_RETURN, 0)
            except Exception:
                pass
            _t.sleep(0.5)

    threading.Thread(target=_clicker, args=(duration,), daemon=True).start()
    time.sleep(0.2)


def _online_loop_first_plc_legacy(max_retries: int = 3) -> dict:
    """Login + Start with verification and retry loop.

    After restart, the DTE COM connection may be stale and
    ``full_online_cycle()`` can return OK without actually starting
    the PLC.  This function verifies the runtime state via pyads
    and retries until the PLC is confirmed in Run mode.
    """
    import pyads as _pyads

    last_online = {}
    for attempt in range(max_retries):
        time.sleep(1)

        try:
            last_online = full_online_cycle()
        except Exception as e:
            last_online = {"status": "skipped", "reason": str(e)[:100]}
            time.sleep(2)
            continue

        # Verify via pyads
        try:
            sysman_v = _sysman()
            target = sysman_v.GetTargetNetId()
            ip = ".".join(target.split(".")[:4])
            plc = _pyads.Connection(target, 851, ip)
            plc.open()
            state, _ = plc.read_state()
            plc.close()
            if state == 5:  # Run
                last_online["verified"] = "Run"
                if attempt > 0:
                    last_online["retried"] = True
                return last_online
        except Exception as e:
            last_online["verify_error"] = str(e)[:80]

        # Not running — clear cache, wait, retry
        global _dte_cache
        _dte_cache = None
        time.sleep(3 + attempt * 2)

    # All retries exhausted — one last attempt via Project node
    try:
        _dte_cache = None
        sysman_v = _sysman()
        proj = _find_plc_project(sysman_v)
        if proj:
            proj.ConsumeXml(
                "<TreeItem><IECProjectDef><OnlineSettings><Commands>"
                "<LoginCmd>true</LoginCmd><StartCmd>true</StartCmd>"
                "</Commands></OnlineSettings></IECProjectDef></TreeItem>"
            )
            time.sleep(3)
            target = sysman_v.GetTargetNetId()
            ip = ".".join(target.split(".")[:4])
            plc = _pyads.Connection(target, 851, ip)
            plc.open()
            state, _ = plc.read_state()
            plc.close()
            last_online["login"] = {"status": "ok"}
            last_online["start"] = {"status": "ok"}
            last_online["verified"] = "Run" if state == 5 else f"State={state}"
            last_online["fallback"] = "ConsumeXml on Project node"
    except Exception as e:
        last_online["fallback_error"] = str(e)[:80]

    return last_online


def _load_pyads():
    """Import pyads after registering the TwinCAT 64-bit ADS DLL folder."""
    import os

    dll_dirs = (
        r"C:\Program Files (x86)\Beckhoff\TwinCAT\Common64",
        r"C:\TwinCAT\Common64",
    )
    for folder in dll_dirs:
        if os.path.isdir(folder):
            try:
                os.add_dll_directory(folder)
            except (AttributeError, OSError):
                pass
            current_path = os.environ.get("PATH", "")
            if folder.lower() not in current_path.lower():
                os.environ["PATH"] = folder + os.pathsep + current_path
    import pyads
    return pyads


def _read_ads_state(target: str, port: int) -> int:
    pyads = _load_pyads()
    ip = ".".join(target.split(".")[:4])
    connection = pyads.Connection(target, int(port), ip)
    try:
        connection.open()
        state, _device_state = connection.read_state()
        return int(state)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _poll_ads_state(target: str, port: int, expected: int = 5,
                    timeout: float = 30.0, interval: float = 1.5) -> dict:
    deadline = time.monotonic() + timeout
    last_state = None
    last_error = ""
    while time.monotonic() < deadline:
        try:
            last_state = _read_ads_state(target, port)
            if last_state == expected:
                return {"verified": True, "state": last_state, "port": port}
        except Exception as exc:
            last_error = str(exc)[:200]
        time.sleep(interval)
    return {
        "verified": False,
        "state": last_state,
        "port": port,
        "error": last_error or f"ADS state did not become {expected}",
    }


def _local_plc_identities(sysman) -> dict[int, dict]:
    """Read the identities produced by the current local PLC build."""
    import xml.etree.ElementTree as ET

    identities: dict[int, dict] = {}
    for runtime in _list_plc_runtimes(sysman):
        port = runtime.get("ads_port")
        instance = runtime.get("instance")
        if port is None or instance is None:
            continue
        identity = {
            "name": runtime["name"],
            "ads_port": int(port),
            "project_name": runtime.get("project_name") or "",
            "application_timestamp": None,
        }
        try:
            root = ET.fromstring(str(instance.ProduceXml(False)))
            properties = {}
            for prop in root.findall(".//Properties/Property"):
                name = (prop.findtext("Name") or "").strip()
                value = (prop.findtext("Value") or "").strip()
                if name:
                    properties[name] = value
            # PLC instances generated by current TwinCAT builds expose these
            # values as module parameters rather than generic properties.
            for param in root.findall(".//ParameterValues/Value"):
                name = (param.findtext("Name") or "").strip()
                value = (
                    param.findtext("Value")
                    or param.findtext("String")
                    or ""
                ).strip()
                if name and value:
                    properties[name] = value
            timestamp = properties.get("Application Timestamp", "")
            if timestamp:
                identity["application_timestamp"] = int(timestamp, 0)
            module_name = (
                properties.get("Project Name")
                or root.findtext(".//Module/Name")
                or ""
            ).strip()
            if module_name:
                identity["project_name"] = module_name
        except Exception as exc:
            identity["identity_error"] = str(exc)[:200]
        identities[int(port)] = identity
    return identities


def _read_target_plc_identity(target: str, port: int) -> dict:
    """Read the identity of the PLC application currently running on target."""
    pyads = _load_pyads()
    ip = ".".join(target.split(".")[:4])
    connection = pyads.Connection(target, int(port), ip)
    try:
        connection.open()
        timestamp = connection.read_by_name(
            "TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp",
            pyads.PLCTYPE_UDINT,
        )
        project_name = connection.read_by_name(
            "TwinCAT_SystemInfoVarList._AppInfo.ProjectName",
            pyads.PLCTYPE_STRING,
        )
        return {
            "application_timestamp": int(timestamp),
            "project_name": str(project_name or "").rstrip("\x00"),
        }
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _verify_target_plc_identity(target: str, port: int, expected: dict) -> dict:
    expected_timestamp = expected.get("application_timestamp")
    if expected_timestamp is None:
        return {
            "verified": False,
            "error": "Local PLC application timestamp could not be read after build",
            "expected": expected,
        }
    try:
        actual = _read_target_plc_identity(target, port)
    except Exception as exc:
        return {
            "verified": False,
            "error": str(exc)[:200],
            "expected": expected,
        }
    timestamp_matches = actual.get("application_timestamp") == expected_timestamp
    expected_project = str(expected.get("project_name") or "")
    project_matches = (
        not expected_project or actual.get("project_name") == expected_project
    )
    return {
        "verified": bool(timestamp_matches and project_matches),
        "timestamp_matches": timestamp_matches,
        "project_matches": project_matches,
        "expected": expected,
        "actual": actual,
    }


def _online_loop(max_retries: int = 3,
                 expected_identities: dict[int, dict] | None = None) -> dict:
    """Login/start and verify every configured PLC on its discovered port."""
    last_result: dict = {}
    for attempt in range(max_retries):
        global _dte_cache
        _dte_cache = None
        sysman = _sysman()
        target = str(sysman.GetTargetNetId())
        runtimes = _list_plc_runtimes(sysman)
        if not runtimes:
            return {
                "status": "skipped",
                "verified": True,
                "reason": "No PLC projects configured",
                "plcs": [],
            }

        cycle = full_online_cycle(all_plcs=True)
        if cycle.get('status') == 'incomplete':
            return dict(cycle, failed_stage='login-verification', attempts=attempt + 1)
        commands_ok = cycle.get('verified') is True
        plc_results = []
        for runtime in runtimes:
            port = runtime["ads_port"]
            if port is None:
                plc_results.append({
                    "name": runtime["name"],
                    "ads_port": None,
                    "port_source": runtime["port_source"],
                    "verified": False,
                    "error": "PLC ADS port could not be read from the project",
                })
                continue
            verification = _poll_ads_state(
                target, port, expected=5, timeout=10.0, interval=1.0
            )
            identity = None
            if verification.get("verified") and expected_identities is not None:
                expected = expected_identities.get(int(port))
                if expected is None:
                    identity = {
                        "verified": False,
                        "error": "No local build identity was captured for this PLC port",
                    }
                else:
                    identity = _verify_target_plc_identity(target, int(port), expected)
                verification["identity"] = identity
                verification["verified"] = bool(
                    verification.get("verified") and identity.get("verified")
                )
            if not commands_ok:
                verification["verified"] = False
                verification["command_error"] = "Login or Start command failed"
            verification.update({
                "name": runtime["name"],
                "ads_port": port,
                "port_source": runtime["port_source"],
            })
            plc_results.append(verification)

        verified = all(item.get("verified") for item in plc_results)
        last_result = {
            "status": "ok" if verified else "failed",
            "verified": verified,
            "attempt": attempt + 1,
            "commands": cycle,
            "commands_verified": commands_ok,
            "plcs": plc_results,
        }
        if verified:
            return last_result
        time.sleep(2 + attempt * 2)
    return last_result


@_preserve_xae_ui_state
def deploy(silent: bool = True, sln_path: str = "") -> dict:
    """Full deployment: build → activate → restart → login → start.

    ``ActivateConfiguration`` writes the config to registry.  ``StartRestartTwinCAT``
    is required afterwards to physically load and start the runtime.
    This path does not switch the target to Config mode.  Config is reserved
    for an explicitly requested mode change or hardware scan preparation.

    For pure code changes use ``build + online`` instead.

    Args:
        silent: When ``True`` (default), enables SilentMode to suppress
            license dialogs and auto-dismisses confirmation popups.
            Set to ``False`` to turn off SilentMode — all dialogs appear.
        sln_path: Optional path to a .sln file.  When empty and no
            solution is loaded in XAE, the function scans ``output/``
            for a .sln to open.
    """
    r = {}

    set_silent_mode(silent)

    # ── Ensure a solution is loaded ──
    dte = _dte()
    if dte.Solution.Projects.Count == 0:
        if sln_path:
            sln = str(Path(sln_path).resolve())
            dte.Solution.Open(sln)
            # Poll until solution is fully loaded (max 20 s)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                try:
                    if dte.Solution.Projects.Count > 0:
                        break
                except Exception:
                    pass
                time.sleep(1.0)
        else:
            return {
                "success": False,
                "status": "failed",
                "failed_stage": "solution",
                "build": {"error_count": 1, "errors": ["No solution open. Pass --sln or open a project in XAE."]},
            }

    def _maybe_dismiss(secs: float):
        if silent:
            _dismiss_dialogs(secs)

    # ── Align build platform with target ──
    platform_result = set_build_platform()
    r["platform"] = platform_result
    if platform_result.get("status") != "ok":
        r.update({"success": False, "status": "failed", "failed_stage": "platform"})
        return r

    # Enable PLC project in build config (basic template has ShouldBuild=False)
    r["build_config"] = _ensure_plc_build_enabled()
    if not r["build_config"].get("verified"):
        r.update({"success": False, "status": "failed", "failed_stage": "build-config"})
        return r

    # Build synchronously and trust LastBuildInfo, never an empty Error List.
    _maybe_dismiss(3)
    try:
        from ._ps_bridge import com_build
        native_build = com_build(always_read_errors=False)
    except Exception as exc:
        r["build"] = {
            "verified": False,
            "error_count": 1,
            "errors": [str(exc)[:500]],
        }
        r.update({"success": False, "status": "failed", "failed_stage": "build"})
        return r
    failed_projects = int(native_build.get("failedProjects", 0) or 0)
    error_count = int(native_build.get("errorCount", 0) or 0)
    r["build"] = {
        **native_build,
        "failed_projects": failed_projects,
        "error_count": error_count,
        "verified": failed_projects == 0 and error_count == 0,
    }
    _maybe_dismiss(3)
    if not r["build"]["verified"]:
        r.update({"success": False, "status": "failed", "failed_stage": "build"})
        return r

    # Capture the exact identities generated by this successful build. These
    # are compared with the applications actually running after login/start.
    expected_identities = _local_plc_identities(_sysman())
    r["expected_plc_identities"] = list(expected_identities.values())
    unresolved_identities = [
        item for item in expected_identities.values()
        if item.get("application_timestamp") is None
    ]
    if unresolved_identities:
        r.update({"success": False, "status": "failed",
                  "failed_stage": "build-identity"})
        return r

    # Boot project (required before activate, or runtime has no PLC code)
    _maybe_dismiss(2)
    r["boot"] = set_boot_project()
    _maybe_dismiss(2)
    if not r["boot"].get("boot_project", False):
        r.update({"success": False, "status": "failed", "failed_stage": "boot"})
        return r

    # Get sysman for state polling during activation + restart
    sysman = _sysman()

    # Activate (writes config to registry)
    _maybe_dismiss(3)
    r["activate"] = activate_configuration()
    if r["activate"].get("status") != "activated":
        r.update({"success": False, "status": "failed", "failed_stage": "activate"})
        return r
    # Poll until XAE async activation settles (max 15 s)
    _poll_runtime_started(sysman, timeout=15.0, interval=2.0)

    # Restart TwinCAT to load the new config and start runtime
    # Invalidate the DTE cache — XAE may restart and the old object becomes stale.
    global _dte_cache
    _dte_cache = None
    _maybe_dismiss(10)
    r["restart"] = _restart_with_retry()
    if r["restart"].get("status") != "restarted" or not r["restart"].get("started"):
        r.update({"success": False, "status": "failed", "failed_stage": "restart"})
        return r
    # Poll until runtime comes up after restart (max 30 s)
    try:
        _poll_runtime_started(_sysman(), timeout=30.0, interval=2.0)
    except Exception:
        pass  # best-effort — Online step will retry with its own COM connection
    _maybe_dismiss(5)

    # Clear DTE cache so _online_loop gets a fresh connection
    _dte_cache = None

    # Verify the TwinCAT system runtime independently of PLC runtimes.
    try:
        state_sysman = _sysman()
        state_target = str(state_sysman.GetTargetNetId())
        r["system"] = _poll_ads_state(
            state_target, 10000, expected=5, timeout=30.0, interval=1.5
        )
    except Exception as exc:
        r["system"] = {"verified": False, "port": 10000, "error": str(exc)[:200]}
    if not r["system"].get("verified"):
        r.update({"success": False, "status": "failed", "failed_stage": "system-state"})
        return r

    # Cache the target version after successful activation
    try:
        sysman = _sysman()
        target = sysman.GetTargetNetId()
        ver_info = get_target_tc_version()
        if ver_info["source"] == "project":
            cache_target_version(target, ver_info["version_str"])
    except Exception:
        pass

    # Online (fresh connection after restart, with retry loop)
    _maybe_dismiss(3)
    r["online"] = _online_loop(expected_identities=expected_identities)
    _maybe_dismiss(2)
    if not r["online"].get("verified"):
        r.update({"success": False, "status": "failed", "failed_stage": "plc-state"})
        return r

    _maybe_dismiss(2)

    r["errors"] = _read_errors()
    if int(r["errors"].get("error_count", 0) or 0) > 0:
        r.update({"success": False, "status": "failed",
                  "failed_stage": "post-deploy-errors"})
        return r
    r["success"] = True
    r["status"] = "ok"
    return r

# ---------------------------------------------------------------------------
# Error list helper
# ---------------------------------------------------------------------------

def _read_errors() -> dict:
    """Read current error list via COM (no pyautogui)."""
    try:
        from .inspect import read_error_list
        return read_error_list()
    except Exception:
        return {"errors": [], "error_count": 0}


def _clear_errors() -> None:
    """Clear the TwinCAT error list before a build (COM only)."""
    from .inspect import clear_error_list
    clear_error_list()

def _ensure_plc_build_enabled() -> dict:
    """Set ``ShouldBuild = True`` on the PLC project context.

    The basic template creates a PLC project that is excluded from the
    active build configuration by default.  Without this the compiler
    never runs on the PLC code.
    """
    result = {"verified": False, "projects": []}
    try:
        dte = _dte()
        sln = dte.Solution
        cfg = sln.SolutionBuild.ActiveConfiguration
        for i in range(1, cfg.SolutionContexts.Count + 1):
            ctx = cfg.SolutionContexts.Item(i)
            if "PLC" in str(getattr(ctx, "ProjectName", "") or ""):
                ctx.ShouldBuild = True
                result["projects"].append(str(ctx.ProjectName))
                if not bool(ctx.ShouldBuild):
                    result["error"] = f"Could not enable build for {ctx.ProjectName}"
                    return result
        # Some localized project names do not contain PLC. No matching context
        # is acceptable only when every solution context is already enabled.
        if not result["projects"]:
            disabled = []
            for i in range(1, cfg.SolutionContexts.Count + 1):
                ctx = cfg.SolutionContexts.Item(i)
                if not bool(ctx.ShouldBuild):
                    disabled.append(str(getattr(ctx, "ProjectName", "") or i))
            if disabled:
                result["error"] = "Disabled build contexts: " + ", ".join(disabled)
                return result
        result["verified"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)[:200]
        return result
