"""Validate the curated TwinCAT Agent knowledge catalog and optional FTS5 DB.

The check is intentionally dependency-light. PyYAML is already a project
dependency; SQLite validation only opens the database read-only and never
changes the shipped index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml


REQUIRED_SOURCE_FIELDS = {
    "id",
    "path",
    "kind",
    "authority",
    "status",
    "language",
    "topics",
    "source_reference",
    "verification",
}
ALLOWED_KINDS = {
    "curated-summary",
    "implementation-contract",
    "training-summary",
    "operational-runbook",
    "primary-source",
}
ALLOWED_AUTHORITIES = {
    "official-manual-summary",
    "official-validated-contract",
    "internal-training-material",
    "repository-observed",
    "official-manual",
}
ALLOWED_STATUSES = {"active", "draft", "deprecated"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_catalog(catalog_path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取 catalog: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError("catalog 必须包含 sources 列表")
    return data


def validate_catalog(repo_root: Path | None = None, catalog_path: Path | None = None) -> list[str]:
    """Return human-readable validation errors for the curated source layer."""
    root = (repo_root or _repo_root()).resolve()
    catalog = (catalog_path or root / "knowledge_base" / "catalog.yaml").resolve()
    errors: list[str] = []
    try:
        data = _load_catalog(catalog)
    except ValueError as exc:
        return [str(exc)]

    kinds = set((data.get("source_kinds") or {}).keys())
    authorities = set((data.get("authority_levels") or {}).keys())
    ids: set[str] = set()
    paths: set[str] = set()
    listed: set[str] = set()

    for index, source in enumerate(data["sources"], start=1):
        prefix = f"sources[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        missing = sorted(REQUIRED_SOURCE_FIELDS - set(source))
        if missing:
            errors.append(f"{prefix} 缺少字段: {', '.join(missing)}")
            continue
        source_id = str(source["id"])
        relative = str(source["path"]).replace("\\", "/")
        if source_id in ids:
            errors.append(f"重复 source id: {source_id}")
        ids.add(source_id)
        if relative in paths:
            errors.append(f"重复 source path: {relative}")
        paths.add(relative)
        listed.add(relative)

        source_path = (root / relative).resolve()
        try:
            source_path.relative_to(root)
        except ValueError:
            errors.append(f"路径越界: {relative}")
            continue
        if not source_path.is_file():
            errors.append(f"文件不存在: {relative}")
            continue
        kind = str(source["kind"])
        authority = str(source["authority"])
        if kind not in ALLOWED_KINDS or kind not in kinds:
            errors.append(f"{relative}: 未知 kind {kind}")
        if authority not in ALLOWED_AUTHORITIES or authority not in authorities:
            errors.append(f"{relative}: 未知 authority {authority}")
        if str(source["status"]) not in ALLOWED_STATUSES:
            errors.append(f"{relative}: 未知 status {source['status']}")
        if not isinstance(source["topics"], list) or not source["topics"]:
            errors.append(f"{relative}: topics 不能为空列表")
        if source_path.suffix.lower() == ".pdf":
            pages = source.get("pages")
            if not isinstance(pages, int) or pages <= 0:
                errors.append(f"{relative}: PDF 必须声明正整数 pages")
        expected_hash = source.get("sha256")
        if expected_hash:
            actual_hash = _sha256(source_path)
            if str(expected_hash).upper() != actual_hash:
                errors.append(f"{relative}: sha256 不匹配，catalog={expected_hash}, actual={actual_hash}")
        generated_by = source.get("generated_by")
        if generated_by and not (root / str(generated_by)).is_file():
            errors.append(f"{relative}: generated_by 不存在: {generated_by}")

    discovered = {
        path.relative_to(root).as_posix()
        for path in (root / "knowledge_base").rglob("*")
        if path.is_file()
        and path.name.lower() != "readme.md"
        and path.suffix.lower() in {".md", ".pdf"}
    }
    for missing in sorted(discovered - listed):
        errors.append(f"未登记知识文件: {missing}")
    for stale in sorted(listed - discovered):
        errors.append(f"catalog 中的文件未被发现: {stale}")
    return errors


def validate_index(db_path: Path) -> list[str]:
    """Check the minimum read-only contract of the shipped FTS5 database."""
    errors: list[str] = []
    if not db_path.is_file():
        return [f"索引不存在: {db_path}"]
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        required = {"docs", "docs_fts"}
        missing = sorted(required - tables)
        if missing:
            errors.append(f"索引缺少表: {', '.join(missing)}")
        else:
            docs_count = int(connection.execute("SELECT count(*) FROM docs").fetchone()[0])
            fts_count = int(connection.execute("SELECT count(*) FROM docs_fts").fetchone()[0])
            if docs_count <= 0:
                errors.append("docs 表为空")
            if docs_count != fts_count:
                errors.append(f"docs/docs_fts 数量不一致: {docs_count}/{fts_count}")
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"索引读取失败: {exc}")
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 TwinCAT Agent 知识库目录和 FTS5 索引")
    parser.add_argument("--check-index", action="store_true", help="同时只读检查 data/ba-docs/index.db")
    parser.add_argument("--db", type=Path, help="指定要检查的 SQLite 索引路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    root = _repo_root()
    errors = validate_catalog(root)
    if args.check_index:
        errors.extend(validate_index((args.db or root / "data" / "ba-docs" / "index.db").resolve()))
    result = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("知识库校验失败:")
        for error in errors:
            print(f"- {error}")
    else:
        print("知识库校验通过。" + (" 精选层和 FTS5 索引均正常。" if args.check_index else ""))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
