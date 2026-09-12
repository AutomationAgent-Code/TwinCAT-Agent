"""
TwinCAT Diagnostics Module.

Provides comprehensive health checks, ADS connectivity tests,
route diagnostics, license queries, and enhanced error analysis.

Usage:
    from tc_template.diagnostics import system_health_check
    report = system_health_check()
"""

from __future__ import annotations

import time
import socket
import traceback
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ======================================================================
# 1. ADS Ping — test basic ADS connectivity
# ======================================================================

def ads_ping(target: str = "", timeout: float = 2.0) -> dict:
    """Test ADS connectivity to a target NetId.

    Tries pyads first (AMS port 48898), falls back to raw TCP.
    If *target* is empty, uses the current active target.

    Returns:
        {"reachable": bool, "latency_ms": float, "method": "pyads"|"tcp",
         "target": str, "error": str|None}
    """
    result: dict[str, Any] = {
        "reachable": False,
        "latency_ms": 0.0,
        "method": "",
        "target": "",
        "error": None,
        "timestamp": _now(),
    }

    # Resolve target
    if not target:
        try:
            from tc_template.tc_platform import get_target_net_id
            target = get_target_net_id()
        except Exception:
            result["error"] = "No target specified and could not read current target."
            return result
    result["target"] = target

    # Try pyads
    try:
        import pyads
        route = pyads.open_port()
        t0 = time.monotonic()
        route.read_state(target)
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["reachable"] = True
        result["method"] = "pyads"
        result["ads_port"] = 48898
        route.close()
        return result
    except Exception as e:
        result["error"] = str(e)[:200]

    # Fall back to raw TCP on port 48898
    try:
        # NetId is like "172.16.1.1.1.1" — extract the IP part
        parts = target.split(".")
        if len(parts) >= 4:
            ip = ".".join(parts[:4])
            t0 = time.monotonic()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, 48898))
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["reachable"] = True
            result["method"] = "tcp"
            result["ads_port"] = 48898
            sock.close()
            result["error"] = None
    except Exception as e:
        if result["error"] is None:
            result["error"] = str(e)[:200]

    return result


# ======================================================================
# 2. System Health Check — full diagnostic report
# ======================================================================

def system_health_check(include_error_list: bool = True) -> dict:
    """Run a comprehensive TwinCAT system health check.

    Checks:
      - TwinCAT runtime status
      - XAE (Visual Studio Shell) connectivity
      - Active solution and PLC project
      - Target connectivity (ADS ping)
      - Build platform
      - Static routes
      - Error list (optional)
      - Local TwinCAT version

    Returns a structured dict with a top-level ``healthy`` flag and
    per-section pass/fail/error details.
    """
    report: dict[str, Any] = {
        "healthy": True,
        "timestamp": _now(),
        "sections": {},
    }

    # ── Runtime ───────────────────────────────────────────────────────
    runtime: dict[str, Any] = {"label": "TwinCAT Runtime", "pass": False}
    try:
        from tc_template.tc_platform import get_runtime_state, is_twincat_started
        started = is_twincat_started()
        state = get_runtime_state()
        runtime["started"] = started
        runtime["state"] = state.get("state", "?")
        runtime["pass"] = started
        if not started:
            runtime["suggestion"] = "Try: tc run  or restart TwinCAT manually."
    except Exception as e:
        runtime["error"] = str(e)[:200]
        runtime["pass"] = False
        report["healthy"] = False
    report["sections"]["runtime"] = runtime

    # ── XAE Connectivity ──────────────────────────────────────────────
    xae: dict[str, Any] = {"label": "XAE / Visual Studio Shell", "pass": False}
    try:
        from tc_template.tc_platform import _dte
        dte = _dte()
        xae["version"] = getattr(dte, "Version", "?")
        xae["name"] = getattr(dte, "Name", "TcXaeShell")
        xae["pass"] = True
    except Exception as e:
        xae["error"] = str(e)[:200]
        xae["suggestion"] = "Ensure TwinCAT XAE is running. Start XAE manually if needed."
        report["healthy"] = False
    report["sections"]["xae"] = xae

    # ── Solution / PLC Project ────────────────────────────────────────
    solution: dict[str, Any] = {"label": "Solution & PLC Project", "pass": False}
    try:
        from tc_template.tc_platform import _dte
        dte = _dte()
        sln = dte.Solution
        if sln:
            solution["full_name"] = sln.FullName or "(unnamed)"
            solution["project_count"] = sln.Projects.Count
            # Check for PLC project
            try:
                from tc_template.tc_platform import _sysman
                sysman = _sysman()
                tipc = sysman.LookupTreeItem("TIPC")
                if tipc and len(list(tipc)) > 0:
                    solution["plc_project"] = True
                    solution["pass"] = True
                else:
                    solution["plc_project"] = False
                    solution["pass"] = True  # solution open w/o PLC is OK
                    solution["suggestion"] = "No PLC project found. Create one via Templates tab."
            except Exception:
                solution["plc_project"] = False
                solution["pass"] = True
        else:
            solution["pass"] = False
            solution["suggestion"] = "Open a solution via File > Open, or create one via Templates tab."
    except Exception as e:
        solution["error"] = str(e)[:200]
        solution["pass"] = False
        report["healthy"] = False
    report["sections"]["solution"] = solution

    # ── Target Connectivity ───────────────────────────────────────────
    target_section: dict[str, Any] = {"label": "Target Connectivity", "pass": False}
    try:
        from tc_template.tc_platform import get_target_net_id
        netid = get_target_net_id()
        target_section["netid"] = netid
        ping_result = ads_ping(target=netid, timeout=3.0)
        target_section["reachable"] = ping_result["reachable"]
        target_section["latency_ms"] = ping_result["latency_ms"]
        target_section["method"] = ping_result["method"]
        target_section["pass"] = ping_result["reachable"]
        if not ping_result["reachable"]:
            target_section["suggestion"] = (
                f"Target {netid} not reachable via ADS. "
                "Check: Ethernet cable, power, firewall, and AMS Router on target."
            )
    except Exception as e:
        target_section["error"] = str(e)[:200]
        target_section["pass"] = False
        report["healthy"] = False
    report["sections"]["target"] = target_section

    # ── Routes ────────────────────────────────────────────────────────
    routes: dict[str, Any] = {"label": "Static Routes", "pass": False}
    try:
        from tc_template.tc_platform import list_static_routes
        route_list = list_static_routes()
        routes["count"] = len(route_list)
        routes["routes"] = [
            {"name": r["name"], "net_id": r["net_id"], "address": r["address"],
             "type": r["type"], "flags": r["flags"]}
            for r in route_list
        ]
        routes["pass"] = True
        if not route_list:
            routes["suggestion"] = (
                "No static routes configured. Add routes via Platform > Add Route, "
                "or use 'tc target add --auto-auth'."
            )
    except Exception as e:
        routes["error"] = str(e)[:200]
        routes["pass"] = False
        report["healthy"] = False
    report["sections"]["routes"] = routes

    # ── Build Platform ────────────────────────────────────────────────
    platform: dict[str, Any] = {"label": "Build Platform", "pass": False}
    try:
        from tc_template.tc_platform import get_build_platform
        plat = get_build_platform()
        if plat.get("status") == "ok":
            platform["full"] = plat["full"]
            platform["config"] = plat.get("config", "")
            platform["pass"] = True
        else:
            platform["error"] = plat.get("message", "Unknown")
            platform["suggestion"] = "Use 'tc platform set' to auto-detect."
    except Exception as e:
        platform["error"] = str(e)[:200]
        platform["pass"] = False
    report["sections"]["platform"] = platform

    # ── Version ───────────────────────────────────────────────────────
    version: dict[str, Any] = {"label": "TwinCAT Version", "pass": False}
    try:
        from tc_template.tc_platform import get_local_tc_version
        local = get_local_tc_version()
        version["local"] = local["version_str"]
        version["pass"] = True
        try:
            from tc_template.tc_platform import get_target_tc_version
            target_ver = get_target_tc_version()
            version["target"] = target_ver["version_str"]
            version["source"] = target_ver.get("source", "?")
        except Exception:
            version["target"] = "(not available — open a project or add a route)"
    except Exception as e:
        version["error"] = str(e)[:200]
        version["pass"] = False
    report["sections"]["version"] = version

    # ── Error List (optional) ─────────────────────────────────────────
    if include_error_list:
        errors_section: dict[str, Any] = {"label": "Error List", "pass": False}
        try:
            from tc_template.inspect import read_error_list
            err_result = read_error_list()
            if err_result.get("success"):
                errors_section["error_count"] = err_result["error_count"]
                errors_section["warning_count"] = err_result["warning_count"]
                errors_section["info_count"] = err_result["info_count"]
                errors_section["total"] = len(err_result["errors"])
                errors_section["errors"] = []
                for e in err_result["errors"]:
                    errors_section["errors"].append({
                        "severity": e["severity"],
                        "description": e.get("description", "")[:200],
                        "file": e.get("file", ""),
                        "line": e.get("line", ""),
                    })
                errors_section["pass"] = (err_result["error_count"] == 0)
            else:
                errors_section["pass"] = True  # no solution open = no errors
                errors_section["total"] = 0
        except Exception as e:
            errors_section["error"] = str(e)[:200]
            errors_section["pass"] = True  # couldn't read = not a failure
        report["sections"]["errors"] = errors_section

    # ── Overall ──
    report["healthy"] = all(
        sec.get("pass", False)
        for sec in report["sections"].values()
    )
    return report


# ======================================================================
# 3. Route Diagnostics
# ======================================================================

def route_diagnostics(target: str = "") -> dict:
    """Check the health of a specific route.

    Verifies: route exists in StaticRoutes.xml, ADS ping succeeds,
    TwinCAT runtime state, and reports any issues.

    Args:
        target: Target NetId or name. Empty = current target.

    Returns a dict with per-check pass/fail and a ``healthy`` flag.
    """
    report: dict[str, Any] = {
        "healthy": True,
        "timestamp": _now(),
        "checks": {},
    }

    # Resolve target
    if not target:
        try:
            from tc_template.tc_platform import get_target_net_id
            target = get_target_net_id()
        except Exception:
            report["healthy"] = False
            report["checks"]["resolve"] = {
                "pass": False,
                "error": "No target specified and could not read current target.",
            }
            return report

    report["target"] = target

    # Check 1: route exists in StaticRoutes.xml
    try:
        from tc_template.tc_platform import list_static_routes, resolve_target
        routes = list_static_routes()
        matched = None
        for r in routes:
            if r["net_id"] == target or r["name"].lower() == target.lower() \
               or r["address"] in target:
                matched = r
                break
        if matched:
            report["checks"]["route_exists"] = {
                "pass": True,
                "name": matched["name"],
                "net_id": matched["net_id"],
                "address": matched["address"],
                "type": matched["type"],
            }
        else:
            report["checks"]["route_exists"] = {
                "pass": False,
                "error": f"Target '{target}' not found in StaticRoutes.xml.",
                "suggestion": f"Add it with: tc target add <name> <ip> --auto-auth",
            }
            report["healthy"] = False
    except Exception as e:
        report["checks"]["route_exists"] = {"pass": False, "error": str(e)[:200]}
        report["healthy"] = False

    # Check 2: ADS ping
    ping = ads_ping(target=target)
    report["checks"]["ads_ping"] = {
        "pass": ping["reachable"],
        "latency_ms": ping["latency_ms"],
        "method": ping["method"],
        "error": ping.get("error"),
    }
    if not ping["reachable"]:
        report["healthy"] = False

    # Check 3: runtime state (skip if not reachable)
    if ping["reachable"]:
        try:
            import pyads
            route = pyads.open_port()
            state, info = route.read_state(target)
            ADS_STATES = {0: "Invalid", 1: "Idle", 2: "Reset", 3: "Init",
                          4: "Start", 5: "Run", 6: "Stop", 7: "Config",
                          8: "Reconfig", 15: "Config/CP-Panel"}
            state_name = ADS_STATES.get(state, f"Unknown({state})")
            report["checks"]["ads_state"] = {
                "pass": True,
                "state_code": state,
                "state_name": state_name,
                "device_info": str(info)[:200] if info else None,
            }
            route.close()
        except Exception as e:
            report["checks"]["ads_state"] = {
                "pass": False,
                "error": str(e)[:200],
            }

    return report


# ======================================================================
# 4. License Status
# ======================================================================

def license_status() -> dict:
    """Query TwinCAT license status via COM.

    Reads the license information from the TwinCAT Automation Settings.
    Requires XAE to be running.

    Returns:
        {"success": bool, "licenses": [...], "raw": str|None, "error": str|None}
    """
    result: dict[str, Any] = {
        "success": False,
        "licenses": [],
        "raw": None,
        "error": None,
        "timestamp": _now(),
    }

    try:
        from tc_template.tc_platform import _dte
        dte = _dte()

        # Try to get license info from TcAutomationSettings
        try:
            settings = dte.GetObject("TcAutomationSettings")
            # The settings object may expose license info via properties
            for attr in ("LicenseInfo", "LicensedRuntime", "TrialLicenseDaysLeft",
                         "TcVersion", "IsLicensed"):
                try:
                    val = getattr(settings, attr, None)
                    if val is not None:
                        result[attr.lower()] = str(val)
                except Exception:
                    pass
        except Exception:
            pass

        # Try reading license via ADS
        try:
            from tc_template.tc_platform import get_target_net_id
            target = get_target_net_id()
            # Extract IP from NetId for pyads
            parts = target.split(".")
            if len(parts) >= 4:
                ip = ".".join(parts[:4])
                import pyads
                route = pyads.open_port()
                # Read device name (index group 0xF001) as a basic license check
                try:
                    dev_name, _ = route.read_device_info(target)
                    result["device_name"] = dev_name
                    result["target_reachable"] = True
                except Exception:
                    result["target_reachable"] = False
                route.close()
        except Exception:
            pass

        result["success"] = True
        if not result.get("licenses") and not result.get("islicensed"):
            result["note"] = (
                "Could not retrieve detailed license info via COM. "
                "The TwinCAT XAE trial or runtime license status is available "
                "in the TwinCAT toolbar icon (right-click > About)."
            )

    except Exception as e:
        result["error"] = str(e)[:300]
        result["suggestion"] = "Ensure TwinCAT XAE is running for license queries."

    return result


# ======================================================================
# 5. Target Info — comprehensive target details
# ======================================================================

def target_info(target: str = "") -> dict:
    """Gather comprehensive information about a TwinCAT target.

    Reads: route info, ADS device name, runtime state, CPU info,
    TwinCAT version, and OS info from the target.

    Args:
        target: Target NetId. Empty = current target.
    """
    info: dict[str, Any] = {
        "timestamp": _now(),
        "target": target,
    }

    # Resolve
    if not target:
        try:
            from tc_template.tc_platform import get_target_net_id
            target = get_target_net_id()
            info["target"] = target
        except Exception as e:
            info["error"] = str(e)[:200]
            return info

    # Route match
    try:
        from tc_template.tc_platform import list_static_routes
        for r in list_static_routes():
            if r["net_id"] == target or r["name"].lower() == target.lower():
                info["route_name"] = r["name"]
                info["route_address"] = r["address"]
                info["route_type"] = r["type"]
                parts = r["address"].split(".")
                if len(parts) == 4:
                    info["target_ip"] = r["address"]
                break
    except Exception:
        pass

    # ADS device info
    try:
        parts = target.split(".")
        ip = ".".join(parts[:4]) if len(parts) >= 4 else ""
        if ip:
            import pyads
            route = pyads.open_port()
            try:
                dev_name, ads_info = route.read_device_info(target)
                info["ads_device_name"] = dev_name
                if ads_info:
                    info["ads_device_info"] = str(ads_info)[:200]
            except Exception:
                info["ads_device_name"] = "(not reachable)"
            route.close()
    except Exception:
        pass

    # Runtime state via pyads
    try:
        import pyads
        route = pyads.open_port()
        state, _ = route.read_state(target)
        ADS_STATES = {0: "Invalid", 1: "Idle", 2: "Reset", 3: "Init",
                      4: "Start", 5: "Run", 6: "Stop", 7: "Config",
                      8: "Reconfig", 15: "Config/CP-Panel"}
        info["ads_state_code"] = state
        info["ads_state_name"] = ADS_STATES.get(state, f"Unknown({state})")
        route.close()
    except Exception:
        info["ads_state_name"] = "(not reachable)"

    # CPU type from TIRS (if available)
    try:
        from tc_template.tc_platform import _sysman
        import xml.etree.ElementTree as ET
        sysman = _sysman()
        tirs = sysman.LookupTreeItem("TIRS")
        if tirs:
            xml_str = tirs.ProduceXml(False)
            root = ET.fromstring(xml_str)
            cpu = root.find(".//CPUType")
            if cpu is not None and cpu.text:
                cpu_type = int(cpu.text)
                info["cpu_type"] = cpu_type
                info["cpu_arch"] = "x64 (Intel)" if cpu_type == 86 else "ARM"
            # OS version
            os_elem = root.find(".//OSVersion")
            if os_elem is not None and os_elem.text:
                info["os_version"] = os_elem.text.strip()
            # Image version
            img = root.find(".//ImageVersion")
            if img is not None and img.text:
                info["image_version"] = img.text.strip()
    except Exception:
        pass

    # TcVersion
    try:
        from tc_template.tc_platform import get_target_tc_version, get_local_tc_version
        info["tc_version_target"] = get_target_tc_version()["version_str"]
    except Exception:
        info["tc_version_target"] = "(not available)"
    try:
        info["tc_version_local"] = get_local_tc_version()["version_str"]
    except Exception:
        info["tc_version_local"] = "(not available)"

    return info


# ======================================================================
# 6. Enhanced Error Diagnosis
# ======================================================================

def diagnose_errors() -> dict:
    """Read error list and provide enhanced diagnosis with suggestions.

    Maps common TwinCAT compile errors to suggested fixes based on
    error patterns.

    Returns:
        {"total": int, "errors": [{"severity":, "description":, "diagnosis":}, ...]}
    """
    result: dict[str, Any] = {
        "success": False,
        "total": 0,
        "error_count": 0,
        "warning_count": 0,
        "info_count": 0,
        "errors": [],
        "timestamp": _now(),
    }

    try:
        from tc_template.inspect import read_error_list
        r = read_error_list()
        if not r.get("success"):
            result["message"] = r.get("raw_text", "Could not read error list.")[:300]
            return result

        result["success"] = True
        result["error_count"] = r["error_count"]
        result["warning_count"] = r["warning_count"]
        result["info_count"] = r["info_count"]
        result["total"] = len(r["errors"])

        for e in r["errors"]:
            entry = {
                "severity": e["severity"],
                "code": e.get("code", ""),
                "description": e.get("description", ""),
                "file": e.get("file", ""),
                "line": e.get("line", ""),
                "diagnosis": _diagnose_error_pattern(e.get("description", "")),
            }
            result["errors"].append(entry)

    except Exception as e:
        result["message"] = f"Error reading error list: {e}"

    return result


# ── Error pattern matching ────────────────────────────────────────────

_ERROR_PATTERNS: dict[str, str] = {
    "TwinCAT System": "This is usually caused by a missing or corrupted installation. "
                      "Try repairing TwinCAT via the installer.",

    "ambiguous": "Variable or type name is ambiguous (used in multiple places). "
                 "Use a fully qualified name or add a namespace prefix.",

    "not defined": "Identifier is not declared. Check: (1) spelling, "
                   "(2) is the GVL/POU in scope?, (3) is the library referenced?",

    "type mismatch": "Data type mismatch. Check the expected type and cast if necessary "
                     "(e.g. TO_UDINT, TO_REAL).",

    "cannot convert": "Implicit type conversion not allowed in IEC 61131-3. "
                      "Use explicit conversion functions (TO_*).",

    "division by zero": "Potential division by zero detected. Add a guard: "
                        "IF divisor <> 0 THEN result := a / divisor; END_IF",

    "array index": "Array index out of bounds. Check that the index variable "
                   "stays within the array's declared range.",

    "library": "Library-related error. Try: (1) check library references (plc libraries), "
               "(2) re-scan installed libraries, (3) check placeholder versions.",

    "memory": "Memory allocation issue. Check: (1) retain/persistent variable count, "
              "(2) array sizes, (3) POUs are not too large.",

    "license": "License error. TwinCAT may be running in trial mode or the license is missing. "
               "Check the TwinCAT toolbar icon or run the license manager.",

    "TMC": "TMC file error — the compiled library descriptor is missing or corrupt. "
           "Try rescanning libraries or re-adding the library reference.",

    "ADS": "ADS communication error. Check: (1) route exists and is active, "
           "(2) target is powered on and reachable, (3) ADS port not blocked by firewall.",

    "access violation": "Runtime access violation — likely a NULL pointer. "
                        "Check: (1) interface references are initialized, "
                        "(2) FB instances are not called before INIT, "
                        "(3) AT% addresses point to valid I/O.",
}


def _diagnose_error_pattern(description: str) -> str | None:
    """Match an error description against known patterns and return a suggestion."""
    lower = description.lower()
    for pattern, suggestion in _ERROR_PATTERNS.items():
        if pattern.lower() in lower:
            return suggestion
    return None


# ======================================================================
# 7. Format helpers — produce human-readable output
# ======================================================================

def format_health_report(report: dict) -> str:
    """Format a system health check report as a readable string."""
    lines = [
        "# System Health Check",
        "",
        f"**Overall**: {'✅ Healthy' if report['healthy'] else '❌ Issues detected'}",
        f"**Time**: {report.get('timestamp', '?')}",
        "",
        "## Results",
        "",
        "| Check | Status | Detail |",
        "|-------|--------|--------|",
    ]

    for key, sec in report.get("sections", {}).items():
        status = "✅" if sec.get("pass") else "❌"
        detail = ""
        if "state" in sec:
            detail += f"State=`{sec['state']}` "
        if "error" in sec:
            detail += f"*{sec.get('error', '')[:80]}* "
        if "count" in sec:
            detail += f"{sec['count']} routes "
        if "latency_ms" in sec:
            detail += f"`{sec['latency_ms']}ms` "
        if "error_count" in sec:
            detail += f"{sec['error_count']} err, {sec['warning_count']} warn "
        if "suggestion" in sec:
            detail += sec["suggestion"][:100]
        lines.append(f"| {sec['label']} | {status} | {detail[:150]} |")

    return "\n".join(lines)


def format_route_report(report: dict) -> str:
    """Format a route diagnostic report as a readable string."""
    lines = [
        "# Route Diagnostics",
        "",
        f"**Target**: `{report.get('target', '?')}`",
        f"**Healthy**: {'✅' if report['healthy'] else '❌ Issues found'}",
        "",
    ]
    for name, check in report.get("checks", {}).items():
        status = "✅" if check.get("pass") else "❌"
        detail = " — ".join(
            str(v) for k, v in check.items()
            if k not in ("pass", "error") and v is not None
        )
        if check.get("error"):
            detail += f" | {check['error'][:100]}"
        lines.append(f"- {status} **{name}**: {detail[:150]}")
    return "\n".join(lines)


def format_target_info(info: dict) -> str:
    """Format target info as a readable string."""
    lines = [
        "# Target Information",
        "",
        f"**NetId**: `{info.get('target', '?')}`",
    ]
    mapping = {
        "route_name": "Route Name",
        "route_address": "Route Address",
        "route_type": "Route Type",
        "target_ip": "Target IP",
        "ads_device_name": "ADS Device Name",
        "ads_state_name": "ADS State",
        "cpu_type": "CPU Type",
        "cpu_arch": "CPU Architecture",
        "os_version": "OS Version",
        "image_version": "Image Version",
        "tc_version_target": "TwinCAT (Target)",
        "tc_version_local": "TwinCAT (Local)",
    }
    for key, label in mapping.items():
        if key in info and info[key]:
            lines.append(f"- **{label}**: `{info[key]}`")
    return "\n".join(lines)
