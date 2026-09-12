"""可扩展 TwinCAT 客户案例库。

案例按功能目录存放；每个叶子目录包含 manifest.yaml 与 PLCopen XML。
扫描器合并内置 CaseLibrary 和 ProgramData 客户目录，同 ID 取较高版本，
同版本时客户目录优先。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


REQUIRED_FIELDS = {
    "id", "name", "version", "publisher", "category", "type", "entry"
}


def builtin_root() -> Path:
    return Path(__file__).resolve().parent.parent / "CaseLibrary"


def customer_root() -> Path:
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(program_data) / "TwinCATAgent" / "CaseLibrary"


def case_roots(extra_roots: list[Path] | None = None) -> list[Path]:
    """返回发现顺序：内置在前、客户目录在后，后者同版本优先。"""
    roots = [builtin_root(), customer_root()]
    env_roots = os.environ.get("TWINCAT_AGENT_CASE_ROOTS", "")
    roots.extend(Path(p) for p in env_roots.split(os.pathsep) if p.strip())
    roots.extend(extra_roots or [])
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _version_key(value: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(value))]
    return tuple((nums + [0, 0, 0])[:3])


def validate_case_dir(case_dir: Path) -> dict[str, Any]:
    manifest_path = case_dir / "manifest.yaml"
    errors: list[str] = []
    if not manifest_path.is_file():
        return {"valid": False, "errors": ["缺少 manifest.yaml"], "path": str(case_dir)}
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig")) or {}
    except Exception as exc:
        return {"valid": False, "errors": [f"manifest 解析失败: {exc}"],
                "path": str(case_dir)}

    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        errors.append(f"缺少字段: {', '.join(missing)}")
    entry = case_dir / str(data.get("entry", ""))
    if data.get("type") != "plcopen":
        errors.append(f"当前仅支持 type=plcopen，实际为 {data.get('type')!r}")
    if not entry.is_file():
        errors.append(f"入口文件不存在: {entry.name}")
    elif data.get("type") == "plcopen":
        try:
            ET.parse(entry)
        except Exception as exc:
            errors.append(f"PLCopen XML 无法解析: {exc}")
    if not isinstance(data.get("objects", []), list):
        errors.append("objects 必须是列表")

    return {"valid": not errors, "errors": errors, "path": str(case_dir),
            "manifest": data, "entry": str(entry)}


def discover_cases(extra_roots: list[Path] | None = None,
                   include_invalid: bool = False) -> list[dict[str, Any]]:
    """递归发现案例并按 ID 合并；客户目录或额外目录可覆盖内置同版本。"""
    selected: dict[str, dict[str, Any]] = {}
    for priority, root in enumerate(case_roots(extra_roots)):
        if not root.is_dir():
            continue
        for manifest in sorted(root.rglob("manifest.yaml")):
            result = validate_case_dir(manifest.parent)
            if not result["valid"] and not include_invalid:
                continue
            data = result.get("manifest", {})
            case_id = str(data.get("id") or manifest.parent.name)
            item = {**data, "id": case_id, "path": str(manifest.parent),
                    "entry_path": result.get("entry", ""),
                    "valid": result["valid"], "errors": result["errors"],
                    "root": str(root), "priority": priority}
            old = selected.get(case_id)
            if old is None or (_version_key(item.get("version", "0")), priority) >= (
                    _version_key(old.get("version", "0")), old["priority"]):
                selected[case_id] = item
    return sorted(selected.values(), key=lambda x: (
        str(x.get("publisher", "")).lower(), str(x.get("category", "")).lower(),
        str(x.get("name", "")).lower()))


def get_case(case_id: str, extra_roots: list[Path] | None = None) -> dict[str, Any]:
    for item in discover_cases(extra_roots=extra_roots, include_invalid=True):
        if item["id"].lower() == case_id.lower():
            return item
    raise FileNotFoundError(f"案例不存在: {case_id}")


def find_cases(intent: str, limit: int = 10,
               extra_roots: list[Path] | None = None) -> list[dict[str, Any]]:
    text = intent.lower()
    results = []
    for item in discover_cases(extra_roots=extra_roots):
        score, why = 0, []
        fields = [item.get("id", ""), item.get("name", ""),
                  item.get("description", ""), item.get("category", ""),
                  item.get("subcategory", "")]
        if any(text in str(field).lower() for field in fields):
            score += 8
            why.append("名称或描述")
        hits = [str(k) for k in item.get("keywords", [])
                if str(k).lower() in text or text in str(k).lower()]
        if hits:
            score += 6 + min(len(hits), 3)
            why.append("关键词:" + "/".join(hits))
        for token in re.findall(r"[\w.-]+", text):
            if len(token) >= 2 and any(token in str(field).lower() for field in fields):
                score += 2
        if score:
            results.append({**item, "score": score, "why": "; ".join(why)})
    return sorted(results, key=lambda x: (-x["score"], x["id"]))[:limit]


def install_case(case_id: str, replace: bool = False,
                 build: bool = True) -> dict[str, Any]:
    """导入案例到当前 XAE；replace=True 用于一键更新同名对象。"""
    item = get_case(case_id)
    if not item["valid"]:
        raise ValueError(f"案例无效: {'; '.join(item['errors'])}")
    if item.get("maturity") != "reusable":
        raise ValueError(
            f"{case_id} 当前 maturity={item.get('maturity', 'unset')}，"
            "仅 reusable 案例允许一键导入")

    from . import _ps_bridge as ps
    try:
        ps.com_logout()
    except Exception:
        pass

    existing_libs = {str(lib.get("name")) for lib in ps.com_list_libraries()}
    added_libs, library_warnings = [], []
    for library in item.get("libraries", []) or []:
        if library in existing_libs:
            continue
        try:
            ps.com_add_library(library)
            added_libs.append(library)
        except Exception as exc:
            library_warnings.append(f"{library}: {exc}")

    options = 2 if replace else 3  # Replace / Skip existing objects
    imported = ps.com_import_plcopen(item["entry_path"], options)
    result: dict[str, Any] = {
        "id": case_id, "version": item.get("version"), "mode": "update" if replace else "add",
        "imported": imported, "libraries_added": added_libs,
        "library_warnings": library_warnings,
    }
    if build:
        result["build"] = ps.com_build(always_read_errors=True)
    return result

