"""Normalize and validate TcHmiSrv dynamic ADS symbol contracts before COM."""
from __future__ import annotations

import re


_PRIMITIVES = {
    "BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "USINT", "INT", "UINT",
    "DINT", "UDINT", "LINT", "ULINT", "REAL", "LREAL", "TIME", "LTIME",
    "DATE", "DATE_AND_TIME", "DT", "TOD", "TIME_OF_DAY", "CHAR", "WCHAR",
}


def contract_example() -> dict:
    return {
        "symbols": {
            "ADS.PLC1.GVL_Hmi.bStart": {
                "ACCESS": 3,
                "DOMAIN": "ADS",
                "DYNAMIC": True,
                "MAPPING": "PLC1::GVL_Hmi::bStart",
                "SCHEMA": {"$ref": "tchmi:general#/definitions/BOOL"},
                "USEMAPPING": True,
            }
        },
        "definitions": {},
    }


class HmiDynamicSymbolValidationError(ValueError):
    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = {
            "error_type": "hmi_dynamic_symbol_validation",
            "required_contract": ["DOMAIN=ADS", "DYNAMIC=true", "USEMAPPING=true",
                                  "MAPPING=<runtime>::<PLC symbol>", "SCHEMA=<TcHmi JSON schema>"],
            "complete_example": contract_example(),
            "note": "不会猜测 IndexGroup/IndexOffset；优先使用 tc_hmi_bind_plc 从实际 TMC 生成。",
            **details,
        }


def _canonical_item(name: str, value: object) -> dict:
    if not isinstance(value, dict):
        raise HmiDynamicSymbolValidationError(f"Dynamic symbol '{name}' must be an object.", symbol=name)
    aliases = {
        "access": "ACCESS", "domain": "DOMAIN", "dynamic": "DYNAMIC",
        "mapping": "MAPPING", "schema": "SCHEMA", "usemapping": "USEMAPPING",
        "use_mapping": "USEMAPPING", "type": "TYPE",
    }
    item = {}
    for key, current in value.items():
        canonical = aliases.get(str(key).replace("-", "_").lower(), str(key).upper())
        item[canonical] = current
    if "SCHEMA" not in item and isinstance(item.get("TYPE"), str):
        plc_type = item.pop("TYPE").strip().upper()
        if plc_type in _PRIMITIVES:
            item["SCHEMA"] = {"$ref": f"tchmi:general#/definitions/{plc_type}"}
    else:
        item.pop("TYPE", None)
    return item


def normalize_dynamic_symbols(symbols: object, definitions: object | None = None) -> tuple[dict, dict]:
    if not isinstance(symbols, dict) or not symbols:
        raise HmiDynamicSymbolValidationError("At least one dynamic ADS symbol is required.")
    if definitions is not None and not isinstance(definitions, dict):
        raise HmiDynamicSymbolValidationError("definitions must be an object.")
    normalized = {}
    for raw_name, raw_item in symbols.items():
        name = str(raw_name or "").strip()
        if not re.fullmatch(r"ADS\.[A-Za-z_][A-Za-z0-9_.-]*\.[A-Za-z_][A-Za-z0-9_.\-]*", name):
            raise HmiDynamicSymbolValidationError(
                f"Invalid dynamic ADS symbol name: {name!r}.", symbol=name)
        item = _canonical_item(name, raw_item)
        missing = [key for key in ("DOMAIN", "DYNAMIC", "MAPPING", "SCHEMA", "USEMAPPING")
                   if key not in item]
        if missing:
            raise HmiDynamicSymbolValidationError(
                f"Dynamic symbol '{name}' is incomplete; missing: {', '.join(missing)}.",
                symbol=name, missing_fields=missing)
        if item["DOMAIN"] != "ADS" or item["DYNAMIC"] is not True or item["USEMAPPING"] is not True:
            raise HmiDynamicSymbolValidationError(
                f"Dynamic symbol '{name}' must use DOMAIN=ADS, DYNAMIC=true and USEMAPPING=true.",
                symbol=name)
        runtime = name.split(".", 2)[1]
        mapping = str(item["MAPPING"] or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*::[A-Za-z_][A-Za-z0-9_:.-]*", mapping):
            raise HmiDynamicSymbolValidationError(f"Invalid ADS mapping for '{name}'.", symbol=name)
        if not mapping.startswith(runtime + "::"):
            raise HmiDynamicSymbolValidationError(
                f"Dynamic symbol '{name}' maps to a different Runtime.", symbol=name,
                expected_runtime=runtime)
        if not isinstance(item["SCHEMA"], dict) or not item["SCHEMA"]:
            raise HmiDynamicSymbolValidationError(f"Dynamic symbol '{name}' requires a JSON schema.", symbol=name)
        normalized[name] = item
    return normalized, dict(definitions or {})
