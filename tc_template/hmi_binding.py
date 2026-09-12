"""High-level PLC-to-HMI binding planning from the active XAE and PLC TMC.

The module deliberately keeps TMC/schema discovery separate from the atomic
configuration write implemented by TcCom.ps1.  No route, PLC runtime state or
target configuration is changed here.
"""

from __future__ import annotations

import re
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


_PRIMITIVES = {
    "BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "USINT", "INT", "UINT",
    "DINT", "UDINT", "LINT", "ULINT", "REAL", "LREAL", "TIME", "LTIME", "DATE",
    "DATE_AND_TIME", "DT", "TOD", "TIME_OF_DAY", "CHAR", "WCHAR",
}
_STRING_RE = re.compile(r"^(W?STRING)(?:\((\d+)\))?$", re.IGNORECASE)


class HmiBindingPreconditionError(ValueError):
    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = {"error_type": "hmi_binding_precondition",
                        "stage": "tmc-symbol-preconditions", **details}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next(iter(_children(node, name)), None)


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _comment(node: ET.Element) -> str:
    return _text(_child(node, "Comment")).strip()


def _type_name(node: ET.Element | None) -> str:
    if node is None:
        return ""
    name = _text(node)
    namespace = str(node.attrib.get("Namespace") or "").strip()
    return f"{namespace}.{name}" if namespace else name


def _safe_definition_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "Type"


class TmcSchemaBuilder:
    """Generate TcHmiSrv-compatible JSON schemas from a PLC TMC type graph."""

    def __init__(self, root: ET.Element, runtime_name: str):
        self.runtime_name = runtime_name
        self.prefix = f"ADS-{_safe_definition_part(runtime_name)}."
        self.types: dict[str, ET.Element] = {}
        self.definitions: dict[str, dict[str, Any]] = {}
        self._building: set[str] = set()
        for node in root.iter():
            if _local(node.tag) != "DataType":
                continue
            name_node = _child(node, "Name")
            plain = _text(name_node)
            qualified = _type_name(name_node)
            if plain:
                self.types.setdefault(plain, node)
            if qualified:
                self.types.setdefault(qualified, node)

    def _definition_name(self, type_name: str) -> str:
        return self.prefix + _safe_definition_part(type_name)

    @staticmethod
    def _with_comment(schema: dict[str, Any], comment: str, order: int | None = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if comment:
            metadata["comment"] = comment
        if order is not None:
            metadata["propertyOrder"] = order
        if not metadata:
            return schema
        return {"allOf": [schema, metadata]}

    def schema_for(self, type_node: ET.Element | None, owner: ET.Element | None = None) -> dict[str, Any]:
        type_name = _type_name(type_node)
        if not type_name:
            raise ValueError("TMC item has no type name")
        base_name = type_name.rsplit(".", 1)[-1].upper()
        string_match = _STRING_RE.fullmatch(base_name)
        if string_match:
            length = int(string_match.group(2) or 80)
            definition_name = self._definition_name(f"{string_match.group(1).upper()}-{length}")
            if definition_name not in self.definitions:
                general = "WString" if string_match.group(1).upper() == "WSTRING" else "String"
                self.definitions[definition_name] = {
                    "allOf": [
                        {"maxLength": length},
                        {"$ref": f"tchmi:general#/definitions/{general}"},
                    ]
                }
            schema: dict[str, Any] = {"$ref": f"tchmi:server#/definitions/{definition_name}"}
        elif base_name in _PRIMITIVES:
            schema = {"$ref": f"tchmi:general#/definitions/{base_name}"}
        else:
            schema = self._schema_for_complex(type_name)

        array_info = _child(owner, "ArrayInfo") if owner is not None else None
        if array_info is not None:
            count = int(_text(_child(array_info, "Elements")) or "0")
            if count < 1:
                raise ValueError(f"TMC array '{type_name}' has no positive element count")
            schema = {"type": "array", "items": schema, "minItems": count, "maxItems": count}
        return schema

    def _schema_for_complex(self, type_name: str) -> dict[str, Any]:
        node = self.types.get(type_name)
        if node is None:
            node = self.types.get(type_name.rsplit(".", 1)[-1])
        if node is None:
            raise ValueError(f"Unsupported or missing TMC type: {type_name}")
        definition_name = self._definition_name(type_name)
        if definition_name in self.definitions or definition_name in self._building:
            return {"$ref": f"tchmi:server#/definitions/{definition_name}"}

        self._building.add(definition_name)
        try:
            enum_items = _children(node, "EnumInfo")
            subitems = _children(node, "SubItem")
            if enum_items:
                values: list[int] = []
                options: list[dict[str, Any]] = []
                for item in enum_items:
                    label = _text(_child(item, "Text"))
                    value = int(_text(_child(item, "Enum")), 0)
                    values.append(value)
                    options.append({"label": label, "value": value})
                definition = {"type": "integer", "enum": values, "options": options}
            elif subitems:
                properties: dict[str, Any] = {}
                required: list[str] = []
                for order, item in enumerate(subitems, start=1):
                    name = _text(_child(item, "Name"))
                    if not name:
                        continue
                    property_schema = self.schema_for(_child(item, "Type"), item)
                    properties[name] = self._with_comment(property_schema, _comment(item), order)
                    required.append(name)
                definition = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": properties,
                    "required": required,
                }
            else:
                base_type = _child(node, "BaseType")
                if base_type is None:
                    raise ValueError(f"TMC type has no enum, members or base type: {type_name}")
                definition = self.schema_for(base_type, node)
            self.definitions[definition_name] = definition
        finally:
            self._building.remove(definition_name)
        return {"$ref": f"tchmi:server#/definitions/{definition_name}"}


def parse_tmc_bindings(tmc_file: str | Path, runtime_name: str,
                       symbol_roots: Iterable[str],
                       read_only_symbols: Iterable[str] = ()) -> dict[str, Any]:
    """Return dynamic symbols and reachable schemas for selected PLC roots."""
    path = Path(tmc_file).resolve()
    root = ET.parse(path).getroot()
    roots = [str(value).strip() for value in symbol_roots if str(value).strip()]
    if not roots:
        raise ValueError("At least one PLC symbol root is required")
    readonly = {str(value).strip().casefold() for value in read_only_symbols if str(value).strip()}
    builder = TmcSchemaBuilder(root, runtime_name)
    selected: dict[str, ET.Element] = {}
    for data_area in (node for node in root.iter() if _local(node.tag) == "DataArea"):
        for symbol in _children(data_area, "Symbol"):
            name = _text(_child(symbol, "Name"))
            if any(name.casefold() == item.casefold() or
                   name.casefold().startswith(item.casefold() + ".") for item in roots):
                selected.setdefault(name, symbol)
    if not selected:
        raise ValueError(f"No TMC symbols matched roots: {', '.join(roots)}")

    symbols: dict[str, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []
    for plc_name, node in sorted(selected.items(), key=lambda pair: pair[0].casefold()):
        schema = builder.schema_for(_child(node, "BaseType"), node)
        is_readonly = any(plc_name.casefold() == item or
                          plc_name.casefold().startswith(item + ".") for item in readonly)
        server_name = f"ADS.{runtime_name}.{plc_name}"
        symbols[server_name] = {
            "ACCESS": 1 if is_readonly else 3,
            "DOMAIN": "ADS",
            "DYNAMIC": True,
            "MAPPING": runtime_name + "::" + plc_name.replace(".", "::"),
            "SCHEMA": TmcSchemaBuilder._with_comment(schema, _comment(node)),
            "USEMAPPING": True,
        }
        details.append({
            "plc_symbol": plc_name,
            "server_symbol": server_name,
            "page_expression": f"%s%{server_name}%/s%",
            "mapping": runtime_name + "::" + plc_name.replace(".", "::"),
            "type": _type_name(_child(node, "BaseType")),
            "access": "read" if is_readonly else "read-write",
        })
    return {
        "tmc_file": str(path),
        "symbols": symbols,
        "definitions": builder.definitions,
        "symbol_details": details,
    }


def _find_tmc(solution_file: str | Path, plc_name: str) -> Path:
    solution = Path(solution_file).resolve()
    matches: list[Path] = []
    for tsproj in solution.parent.rglob("*.tsproj"):
        try:
            root = ET.parse(tsproj).getroot()
        except (OSError, ET.ParseError):
            continue
        for node in root.iter():
            if _local(node.tag) != "Project" or str(node.attrib.get("Name") or "").casefold() != plc_name.casefold():
                continue
            relative = str(node.attrib.get("TmcFilePath") or "").strip()
            if relative:
                candidate = (tsproj.parent / Path(relative.replace("\\", "/"))).resolve()
                if candidate.is_file():
                    matches.append(candidate)
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        raise ValueError(f"Expected one TMC for PLC '{plc_name}', found {len(unique)}")
    return unique[0]


def list_tmc_symbols(tmc_file: str | Path) -> list[str]:
    """Return the exact exported PLC symbol names from one saved TMC.

    This is intentionally a small discovery primitive for diagnostics.  It
    does not infer addresses and it does not treat PLC source declarations as
    exported runtime symbols.
    """
    path = Path(tmc_file).resolve()
    root = ET.parse(path).getroot()
    names: set[str] = set()
    for data_area in (node for node in root.iter() if _local(node.tag) == "DataArea"):
        for symbol in _children(data_area, "Symbol"):
            name = _text(_child(symbol, "Name"))
            if name:
                names.add(name)
    return sorted(names, key=str.casefold)


def _saved_binding_matches(project_file, runtime, netid, port, scope, generated):
    """Recognize an already persisted plan without saving/unloading XAE."""
    root = Path(project_file).parent / 'Server'
    snapshots = {}
    def read(path):
        raw = path.read_bytes()
        snapshots[path] = raw
        return json.loads(raw.decode('utf-8-sig'))
    try:
        for selected_scope in (['default', 'remote'] if scope == 'both' else ['default']):
            config = read(root / 'ADS' / f'ADS.Config.{selected_scope}.json')
            matches = [v for k, v in config.get('RUNTIMES', {}).items()
                       if k.casefold() == runtime.casefold()]
            if len(matches) != 1 or matches[0].get('NETID') != netid or matches[0].get('PORT') != port:
                return False
        config = read(root / 'TcHmiSrv' / 'TcHmiSrv.Config.default.json')
        for key, value in generated['symbols'].items():
            if config.get('SYMBOLS', {}).get(key) != value:
                return False
        for key, value in generated['definitions'].items():
            if config.get('DEFINITIONS', {}).get('ADS', {}).get(key) != value:
                return False
        return all(path.read_bytes() == raw for path, raw in snapshots.items())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def bind_plc(project: str = "", plc: str = "", runtime_name: str = "",
             symbol_roots: list[str] | None = None,
             read_only_symbols: list[str] | None = None,
             scope: str = "default", apply: bool = False) -> dict[str, Any]:
    """Plan/apply an atomic HMI binding to one explicitly resolved PLC runtime."""
    from . import _ps_bridge as ps

    if scope not in {"default", "both"}:
        raise ValueError("scope must be default or both")
    project_info = ps.com_hmi_project_info(project)
    plc_info = ps.ps_com("plc-runtimes")
    runtimes = list(plc_info.get("plcs") or [])
    if plc:
        matches = [item for item in runtimes if str(item.get("name") or "").casefold() == plc.casefold()]
        if len(matches) != 1:
            raise ValueError(f"PLC '{plc}' was not found uniquely; available: " +
                             ", ".join(str(item.get("name") or "") for item in runtimes))
        selected = matches[0]
    else:
        if len(runtimes) != 1:
            raise ValueError("Multiple/no PLC runtimes are open; plc is required")
        selected = runtimes[0]
    port = selected.get("ads_port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"ADS port is unresolved for PLC '{selected.get('name')}'")
    netid = str(plc_info.get("target_netid") or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){5}", netid):
        raise ValueError(f"Invalid or unresolved target AMS NetId: {netid}")

    ads_info = ps.com_hmi_ads_info(project_info["project_file"])
    configured = [item for item in ads_info.get("runtimes") or [] if item.get("scope") == "default"]
    if not runtime_name:
        names = list(dict.fromkeys(str(item.get("name") or "") for item in configured if item.get("name")))
        runtime_name = names[0] if len(names) == 1 else "PLC1"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", runtime_name):
        raise ValueError(f"Invalid HMI ADS Runtime name: {runtime_name}")

    try:
        tmc = _find_tmc(project_info["solution"], str(selected["name"]))
        generated = parse_tmc_bindings(
            tmc, runtime_name, symbol_roots or ["GVL_Hmi"], read_only_symbols or [],
        )
    except (OSError, ET.ParseError, ValueError) as exc:
        raise HmiBindingPreconditionError(
            str(exc), project_file=project_info.get("project_file"),
            solution=project_info.get("solution"), plc=str(selected.get("name") or ""),
            xae_pid=ps._TOOL_TARGET_PID.get() or None,
        ) from exc
    if _saved_binding_matches(project_info['project_file'], runtime_name, netid, port, scope, generated):
        result = {'status': 'unchanged' if apply else 'preview',
                  'configuration_changed': False, 'write_performed': False,
                  'project_reloaded': False, 'apply_required': False,
                  'load_mode': 'reuse-saved-mapping', 'saved_configuration_verified': True,
                  'plc_online_tested': False, 'server_health_tested': False,
                  'dirty_unknown': True,
                  'next_action': 'Use tc_hmi_controls_batch to bind controls to these existing symbols; no project reload is required.'}
    else:
        result = ps.ps_com(
            "hmi-bind-plc-apply",
            project=project_info["project_file"], runtime=runtime_name, netid=netid, port=port,
            scope=scope, symbols=generated["symbols"], definitions=generated["definitions"],
            apply=bool(apply),
        )
    result.update({
        "precondition_stage": "tmc-symbol-preconditions",
        "preconditions_verified": True,
        "xae_pid": ps._TOOL_TARGET_PID.get() or None,
        "solution": project_info.get("solution"),
        "project_file": project_info.get("project_file"),
        "plc": str(selected["name"]),
        "plc_instance": str(selected.get("instance") or ""),
        "target_netid": netid,
        "ads_port": port,
        "port_source": str(selected.get("port_source") or ""),
        "tmc_file": generated["tmc_file"],
        "symbol_count": len(generated["symbols"]),
        "definition_count": len(generated["definitions"]),
        "symbol_details": generated["symbol_details"],
        "page_expression_examples": [
            item["page_expression"] for item in generated["symbol_details"][:12]
        ],
        "binding_format_contract": {
            "page_expression": "%s%ADS.<Runtime>.<PLC symbol>%/s%",
            "server_symbol_key": "ADS.<Runtime>.<PLC symbol>",
            "ads_mapping_only": "<Runtime>::<PLC symbol>",
            "warning": "MAPPING is server configuration and must never be pasted into a page SymbolExpression.",
        },
        "activation_performed": False,
        "server_restart_performed": False,
    })
    return result
