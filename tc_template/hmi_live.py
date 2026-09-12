"""Read-only online verification for HMI ADS mappings."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_ADS_ERROR_NAMES = {
    6: "ERR_TARGETPORTNOTFOUND",
    7: "ERR_TARGETMACHINENOTFOUND",
    13: "ERR_PORTNOTCONNECTED",
    18: "ERR_PORTDISABLED",
    26: "ERR_HOSTUNREACHABLE",
}


def _ads_error_details(message: str) -> dict[str, Any]:
    """Extract a stable ADS diagnosis even when a console mangles CJK text."""
    match = re.search(r"\bADS\s+(\d+)\b", str(message or ""), re.IGNORECASE)
    if not match:
        return {"ads_error_code": None, "ads_error_name": ""}
    code = int(match.group(1))
    return {"ads_error_code": code, "ads_error_name": _ADS_ERROR_NAMES.get(code, "")}


def _select_one(items: list[dict], selector: str, label: str) -> dict:
    if selector:
        matches = [item for item in items if str(item.get("name") or "").casefold() == selector.casefold()]
        if len(matches) != 1:
            raise ValueError(f"{label} '{selector}' was not found uniquely")
        return matches[0]
    if len(items) != 1:
        names = ", ".join(str(item.get("name") or "") for item in items)
        raise ValueError(f"Multiple/no {label} entries are available; specify one ({names})")
    return items[0]


def _schema_type(schema: object, runtime: str) -> str | None:
    if not isinstance(schema, dict):
        return None
    reference = schema.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        prefix = f"ADS-{runtime}."
        if name.startswith(prefix):
            name = name[len(prefix):]
            string_match = re.fullmatch(r"(W?STRING)-(\d+)", name, re.IGNORECASE)
            return f"{string_match.group(1).upper()}({string_match.group(2)})" if string_match else name
        if "tchmi:general#/definitions/" in reference:
            return name
    for item in schema.get("allOf") or []:
        found = _schema_type(item, runtime)
        if found:
            return found
    return None


def explain_browser_binding_mismatch(result: dict, bindings: dict) -> dict:
    """Compare observed runtime errors with saved references, without writing."""
    saved = {}
    for row in bindings.get('bindings') or []:
        saved.setdefault(row.get('control'), set()).add(row.get('expression'))
    mismatches = []
    for viewport in result.get('viewport_results') or []:
        for diagnostic in viewport.get('diagnostics') or []:
            match = re.search(r'Control=([^,\]]+).*?Symbol=(%s%.*?%/s%)',
                              diagnostic.get('message', ''))
            if match and match[1] in saved and match[2] not in saved[match[1]]:
                mismatches.append({'control': match[1], 'runtime_expression': match[2],
                                   'saved_expressions': sorted(saved[match[1]])})
    if mismatches:
        result = {**result, 'binding_source_mismatch': True,
                  'blocking_stage': 'hmi-runtime-content',
                  'binding_mismatches': mismatches[:32],
                  'recommended_sequence': [{'tool': 'tc_hmi_build', 'mode': 'build',
                      'reason': '运行页使用的表达式与已保存页面不同；确认入口并构建 HMI 后重新验证。不要重复生成 PLC 映射。'}]}
    return result


def _resolve_mapped_member(value: str, available: dict) -> dict | None:
    # Exact mappings win. Only accept a member/index suffix after a registered
    # root; never invent a mapping or infer a static ADS address.
    for item in sorted(available.values(), key=lambda row: len(row['server_symbol']), reverse=True):
        for root in (item['server_symbol'], item['plc_symbol']):
            if not value.casefold().startswith(root.casefold()):
                continue
            suffix = value[len(root):]
            if not re.fullmatch(r'(?:\[-?\d+\]|\.[A-Za-z_][A-Za-z0-9_]*|::[A-Za-z_][A-Za-z0-9_]*)+', suffix):
                continue
            return {**item, 'server_symbol': value,
                    'mapped_root': item['server_symbol'],
                    'plc_symbol': item['plc_symbol'] + suffix.replace('::', '.'),
                    'schema_type': None}
    return None


def check_hmi_ads_online(project: str = "", runtime: str = "", plc: str = "",
                         symbols: list[str] | None = None,
                         max_depth: int = 3, max_symbols: int = 32) -> dict[str, Any]:
    """Verify saved HMI mappings through direct, read-only TwinCAT ADS reads."""
    from . import _ps_bridge as ps
    from tc_agent.ads import AdsStateError, read_ads_state
    from tc_agent.dynamic_ads import DynamicAdsError, read_symbol

    depth = max(0, min(int(max_depth), 8))
    limit = max(1, min(int(max_symbols), 32))
    project_info = ps.com_hmi_project_info(project)
    ads_info = ps.com_hmi_ads_info(project_info["project_file"])
    configured = [item for item in ads_info.get("runtimes", [])
                  if item.get("scope") == "default" and item.get("enabled")]
    hmi_runtime = _select_one(configured, runtime, "enabled default HMI Runtime")

    inventory = ps.ps_com("plc-runtimes")
    plcs = list(inventory.get("plcs") or [])
    if plc:
        plc_runtime = _select_one(plcs, plc, "PLC runtime")
    else:
        port_matches = [item for item in plcs if item.get("ads_port") == hmi_runtime.get("port")]
        if len(port_matches) == 1:
            plc_runtime = port_matches[0]
        else:
            plc_runtime = _select_one(plcs, "", "PLC runtime")

    actual_netid = str(inventory.get("target_netid") or "")
    actual_port = plc_runtime.get("ads_port")
    configuration_match = (
        str(hmi_runtime.get("netid") or "").casefold() == actual_netid.casefold()
        and actual_port is not None
        and int(hmi_runtime.get("port") or 0) == int(actual_port)
    )
    endpoint = {
        "hmi_runtime": str(hmi_runtime.get("name") or ""),
        "configured_netid": str(hmi_runtime.get("netid") or ""),
        "configured_port": int(hmi_runtime.get("port") or 0),
        "plc": str(plc_runtime.get("name") or ""),
        "plc_instance": str(plc_runtime.get("instance") or ""),
        "actual_target_netid": actual_netid,
        "actual_ads_port": int(actual_port) if actual_port is not None else None,
        "port_source": str(plc_runtime.get("port_source") or ""),
        "configuration_match": configuration_match,
    }
    if not configuration_match:
        return {
            "status": "configuration-mismatch", "verified": False,
            "read_only": True, **endpoint,
            "reason": "The HMI Runtime endpoint does not match the selected XAE PLC runtime; no ADS symbols were read.",
        }

    server_path = (Path(project_info["project_directory"]) / "Server" / "TcHmiSrv" /
                   "TcHmiSrv.Config.default.json")
    server = json.loads(server_path.read_text(encoding="utf-8-sig"))
    runtime_name = str(hmi_runtime["name"])
    available: dict[str, dict[str, Any]] = {}
    for name, item in (server.get("SYMBOLS") or {}).items():
        if not isinstance(item, dict) or item.get("DOMAIN") != "ADS" or not item.get("DYNAMIC"):
            continue
        mapping = str(item.get("MAPPING") or "")
        prefix = runtime_name + "::"
        if not mapping.startswith(prefix):
            continue
        plc_symbol = mapping[len(prefix):].replace("::", ".")
        available[name] = {
            "server_symbol": name, "plc_symbol": plc_symbol, "mapping": mapping,
            "hmi_access": item.get("ACCESS"),
            "schema_type": _schema_type(item.get("SCHEMA"), runtime_name),
        }

    requested = [str(value).strip() for value in (symbols or []) if str(value).strip()]
    selected: list[dict[str, Any]] = []
    if requested:
        by_plc = {item["plc_symbol"].casefold(): item for item in available.values()}
        by_server = {name.casefold(): item for name, item in available.items()}
        for value in requested:
            item = by_server.get(value.casefold()) or by_plc.get(value.casefold())
            if item is None:
                item = _resolve_mapped_member(value, available)
            if item is None:
                raise ValueError(f"HMI dynamic ADS symbol is not configured: {value}")
            if item not in selected:
                selected.append(item)
    else:
        selected = sorted(available.values(), key=lambda item: item["server_symbol"].casefold())
    if not selected:
        raise ValueError(f"No dynamic ADS symbols are configured for HMI Runtime '{runtime_name}'")
    if len(selected) > limit:
        raise ValueError(f"{len(selected)} symbols selected; max_symbols is {limit}")

    try:
        plc_state = read_ads_state(actual_netid, int(actual_port))
    except AdsStateError as exc:
        error = str(exc)
        return {
            "status": "offline", "verified": False, "read_only": True,
            "error": error, **_ads_error_details(error),
            "requested_symbol_count": len(selected), **endpoint,
        }

    results = []
    for item in selected:
        try:
            response = read_symbol(actual_netid, int(actual_port), item["plc_symbol"], depth=depth)
            actual_type = str((response.get("symbol") or {}).get("type") or "")
            expected_type = str(item.get("schema_type") or "")
            type_match = (actual_type.casefold() == expected_type.casefold()
                          if actual_type and expected_type else None)
            results.append({
                **item, "status": "read", "value": response.get("value"),
                "leaf_values": response.get("leaf_values"), "metadata": response.get("symbol"),
                "type_match": type_match,
            })
        except DynamicAdsError as exc:
            results.append({**item, "status": "failed", "error": str(exc)})
    failed = [item for item in results if item["status"] != "read" or item.get("type_match") is False]
    return {
        "status": "verified" if not failed else "partial-failure",
        "verified": not failed, "read_only": True, "plc_state": plc_state,
        "requested_symbol_count": len(results), "read_count": len(results) - len(failed),
        "failed_count": len(failed), "symbols": results,
        "verification_path": "direct-tcads-symbol-read",
        "hmi_server_involved": False, **endpoint,
    }


def diagnose_hmi_bindings(project: str = "", runtime: str = "", plc: str = "",
                           max_symbols: int = 32) -> dict[str, Any]:
    """Diagnose the complete saved HMI-to-PLC binding chain in one read-only call.

    The result deliberately separates five facts that were previously easy for
    an agent to conflate: expression syntax, saved server mappings, TMC export,
    endpoint configuration, and live ADS reachability.  It never changes the
    XAE target, starts a PLC, reloads HMI, or writes configuration.
    """
    from . import _ps_bridge as ps
    from .hmi_binding import _find_tmc, list_tmc_symbols

    limit = max(1, min(int(max_symbols), 32))
    project_info = ps.com_hmi_project_info(project)
    project_file = str(project_info.get("project_file") or project)
    bindings = ps.com_hmi_bindings(project_file)
    ads_info = ps.com_hmi_ads_info(project_file)
    inventory = ps.ps_com("plc-runtimes")

    causes: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    def cause(code: str, stage: str, message: str, evidence: dict | None = None,
              tools: list[str] | None = None) -> None:
        causes.append({
            "code": code, "stage": stage, "message": message,
            "evidence": evidence or {}, "recommended_tools": tools or [],
        })

    def recommend(tool: str, args: dict, reason: str, mode: str = "read") -> None:
        key = (tool, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if any((item["tool"], json.dumps(item["args"], ensure_ascii=False, sort_keys=True)) == key
               for item in recommendations):
            return
        recommendations.append({"tool": tool, "args": args, "mode": mode, "reason": reason})

    enabled = [item for item in ads_info.get("runtimes", [])
               if item.get("scope") == "default" and item.get("enabled")]
    try:
        hmi_runtime = _select_one(enabled, runtime, "enabled default HMI Runtime")
    except ValueError as exc:
        cause("hmi-runtime-selection-required", "runtime-selection", str(exc),
              {"available": [item.get("name") for item in enabled]}, ["tc_hmi_ads_info"])
        return {
            "status": "blocked", "verified": False, "read_only": True,
            "project": project_info.get("project_name") or bindings.get("project"),
            "project_file": project_info.get("project_file"),
            "binding_count": int(bindings.get("binding_count") or 0),
            "blocking_stage": "runtime-selection", "root_causes": causes,
            "recommended_sequence": recommendations, "write_performed": False,
            "runtime_change_performed": False,
        }

    plcs = list(inventory.get("plcs") or [])
    try:
        if plc:
            plc_runtime = _select_one(plcs, plc, "PLC runtime")
        else:
            port_matches = [item for item in plcs if item.get("ads_port") == hmi_runtime.get("port")]
            plc_runtime = port_matches[0] if len(port_matches) == 1 else _select_one(plcs, "", "PLC runtime")
    except ValueError as exc:
        cause("plc-selection-required", "plc-selection", str(exc),
              {"available": [item.get("name") for item in plcs]}, ["tc_project_info"])
        return {
            "status": "blocked", "verified": False, "read_only": True,
            "project": project_info.get("project_name") or bindings.get("project"),
            "project_file": project_info.get("project_file"),
            "hmi_runtime": hmi_runtime.get("name"),
            "binding_count": int(bindings.get("binding_count") or 0),
            "blocking_stage": "plc-selection", "root_causes": causes,
            "recommended_sequence": recommendations, "write_performed": False,
            "runtime_change_performed": False,
        }

    runtime_name = str(hmi_runtime.get("name") or "")
    actual_netid = str(inventory.get("target_netid") or "")
    actual_port = plc_runtime.get("ads_port")
    endpoint_match = (
        str(hmi_runtime.get("netid") or "").casefold() == actual_netid.casefold()
        and actual_port is not None
        and int(hmi_runtime.get("port") or 0) == int(actual_port)
    )
    endpoint = {
        "hmi_runtime": runtime_name,
        "configured_netid": str(hmi_runtime.get("netid") or ""),
        "configured_port": int(hmi_runtime.get("port") or 0),
        "plc": str(plc_runtime.get("name") or ""),
        "plc_instance": str(plc_runtime.get("instance") or ""),
        "actual_target_netid": actual_netid,
        "actual_ads_port": int(actual_port) if actual_port is not None else None,
        "port_source": str(plc_runtime.get("port_source") or ""),
        "configuration_match": endpoint_match,
    }

    findings = list(bindings.get("findings") or [])
    hard_findings = [item for item in findings if str(item.get("severity") or "").lower() == "error"]
    if hard_findings:
        cause("binding-expression-invalid", "static-bindings",
              f"{len(hard_findings)} 个 HMI 引用缺少 Runtime、内部符号或控件目标。",
              {"findings": hard_findings[:limit]}, ["tc_hmi_bindings", "tc_hmi_read_smart"])

    server_bindings = [item for item in bindings.get("bindings") or []
                       if item.get("kind") == "server" and str(item.get("path") or "")]
    unmapped = [item for item in server_bindings if item.get("state") == "unmapped"]
    mapped = [item for item in server_bindings if item.get("state") == "mapped"]

    tmc_file = ""
    tmc_names: set[str] = set()
    tmc_error = ""
    try:
        tmc_path = _find_tmc(project_info["solution"], str(plc_runtime.get("name") or ""))
        tmc_file = str(tmc_path)
        tmc_names = {name.casefold() for name in list_tmc_symbols(tmc_path)}
    except (KeyError, OSError, ValueError) as exc:
        tmc_error = str(exc)

    def plc_symbol(entry: dict) -> str:
        path = str(entry.get("path") or "")
        prefix = f"ADS.{runtime_name}."
        if path.casefold().startswith(prefix.casefold()):
            return path[len(prefix):]
        short = runtime_name + "."
        return path[len(short):] if path.casefold().startswith(short.casefold()) else path

    missing_from_mapping = sorted({plc_symbol(item) for item in unmapped}, key=str.casefold)
    exported_missing = [name for name in missing_from_mapping if name.casefold() in tmc_names]
    tmc_missing = [name for name in missing_from_mapping if name.casefold() not in tmc_names]
    roots = sorted({name.split(".", 1)[0] for name in exported_missing if name}, key=str.casefold)

    if tmc_error and missing_from_mapping:
        cause("tmc-unavailable", "tmc-export", "无法核对页面变量是否已导出到 PLC TMC。",
              {"error": tmc_error, "symbols": missing_from_mapping[:limit]},
              ["plc_build", "tc_hmi_bind_plc"])
        recommend("plc_build", {}, "重新生成并确认当前 PLC 的 TMC；编译不会自动登录或启动 PLC。")
    if tmc_missing:
        cause("tmc-symbol-missing", "tmc-export",
              f"{len(tmc_missing)} 个页面变量未出现在当前 PLC 的 TMC 中，不能生成可靠 HMI 映射。",
              {"symbols": tmc_missing[:limit], "tmc_file": tmc_file},
              ["plc_structure", "plc_build"])
        recommend("plc_structure", {}, "确认变量声明位于所选 PLC，并核对名称和作用域。")
        recommend("plc_build", {}, "变量修正后重新编译，生成新的 TMC。")
    if exported_missing:
        cause("hmi-mapping-missing", "server-mapping",
              f"{len(exported_missing)} 个变量已在 TMC 导出，但 HMI Server 尚未保存动态映射/Schema。",
              {"symbols": exported_missing[:limit], "tmc_file": tmc_file}, ["tc_hmi_bind_plc"])
        bind_args = {"project": project_info.get("project_name") or bindings.get("project"),
                     "plc": plc_runtime.get("name"), "runtime_name": runtime_name,
                     "symbol_roots": roots, "apply": False}
        recommend("tc_hmi_bind_plc", bind_args,
                  "先预览从实际 TMC 生成的 Runtime、动态符号和 Schema。", "preview")

    if not endpoint_match:
        cause("endpoint-configuration-mismatch", "endpoint-configuration",
              "HMI default Runtime 的 NetId/端口与当前 XAE 所选 PLC Runtime 不一致。",
              endpoint, ["tc_hmi_bind_plc", "tc_target_show"])
        recommend("tc_hmi_bind_plc", {
            "project": project_info.get("project_name") or bindings.get("project"),
            "plc": plc_runtime.get("name"), "runtime_name": runtime_name,
            "symbol_roots": roots or ["GVL_Hmi"], "apply": False,
        }, "使用 XAE 当前真实目标和 PLC ADS 端口预览绑定修复。", "preview")

    online: dict[str, Any]
    mapped_requests = list(dict.fromkeys(str(item.get("path") or "") for item in mapped))
    online_coverage_complete = len(mapped_requests) <= limit
    if mapped_requests and endpoint_match:
        try:
            online = check_hmi_ads_online(
                project=project_file, runtime=runtime_name,
                plc=str(plc_runtime.get("name") or ""),
                symbols=mapped_requests[:limit], max_symbols=limit,
            )
        except (OSError, ValueError) as exc:
            online = {"status": "unavailable", "verified": False, "error": str(exc), **endpoint}
    else:
        online = {
            "status": "not-checked", "verified": False,
            "reason": ("No saved mapped server symbols are referenced by the HMI."
                       if not mapped_requests else "Endpoint configuration mismatch blocks ADS reads."),
            **endpoint,
        }

    if online.get("status") == "offline":
        details = _ads_error_details(str(online.get("error") or ""))
        name = details.get("ads_error_name") or "ADS endpoint unavailable"
        cause("plc-runtime-unreachable", "plc-runtime", f"PLC ADS Runtime 当前不可达：{name}。",
              {**endpoint, **details}, ["tc_state", "tc_target_show", "tc_online"])
        recommend("tc_state", {}, "只读确认当前 TwinCAT System Service 状态。")
    elif online.get("status") == "partial-failure":
        failed = [item for item in online.get("symbols") or []
                  if item.get("status") != "read" or item.get("type_match") is False]
        cause("ads-symbol-read-failed", "online-symbols",
              f"{len(failed)} 个已映射变量在线读取失败或类型不一致。",
              {"symbols": failed[:limit]}, ["tc_hmi_ads_live_check", "tc_hmi_bind_plc"])
    elif mapped_requests and endpoint_match and online.get('verified') is not True:
        cause('online-verification-unavailable', 'online-symbols',
              '在线验证未完成；不能据此断定 Server 映射缺失。',
              {'status': online.get('status'), 'error': online.get('error', '')},
              ['tc_hmi_ads_live_check'])
        recommend('tc_hmi_ads_live_check', {
            'project': project_file, 'runtime': runtime_name,
            'plc': str(plc_runtime.get('name') or ''),
            'symbols': mapped_requests[:limit], 'max_symbols': limit,
        }, '按原始异常核对在线读取；不要反复重建已经存在的映射。')
    elif mapped_requests and endpoint_match and not online_coverage_complete:
        cause("online-coverage-partial", "online-symbols",
              f"页面引用 {len(mapped_requests)} 个已映射变量，本次只读验证限制为 {limit} 个。",
              {"checked": limit, "total": len(mapped_requests)}, ["tc_hmi_ads_live_check"])

    if not server_bindings:
        cause("no-plc-bindings", "static-bindings", "项目中没有已保存的 PLC Server SymbolExpression。",
              {}, ["tc_hmi_bindings"])

    priority = ["static-bindings", "tmc-export", "server-mapping",
                "endpoint-configuration", "plc-runtime", "online-symbols"]
    blocking_stage = next((stage for stage in priority if any(c["stage"] == stage for c in causes)), "")
    static_verified = not hard_findings and not unmapped
    online_verified = online.get("verified") is True and online_coverage_complete
    verified = bool(server_bindings and static_verified and endpoint_match and online_verified)
    return {
        "status": "ready" if verified else "blocked",
        "verified": verified,
        "read_only": True,
        "project": project_info.get("project_name") or bindings.get("project"),
        "project_file": project_info.get("project_file"),
        "solution": project_info.get("solution"),
        "binding_count": int(bindings.get("binding_count") or 0),
        "server_binding_count": len(server_bindings),
        "static_verified": static_verified,
        "unmapped_count": len(unmapped),
        "endpoint": endpoint,
        "tmc": {"status": "available" if tmc_file else "unavailable",
                "file": tmc_file, "error": tmc_error,
                "referenced_missing_count": len(tmc_missing)},
        "online": online,
        "online_coverage_complete": online_coverage_complete and online.get('verified') is True,
        "blocking_stage": blocking_stage,
        "root_causes": causes,
        "recommended_sequence": recommendations,
        "write_performed": False,
        "runtime_change_performed": False,
        "safety_note": "诊断不会切换目标、登录/启动 PLC、重载 HMI 或写入配置。",
    }
