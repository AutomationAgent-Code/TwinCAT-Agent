"""
tc_agent.docsearch — Beckhoff InfoSys 本地文档搜索(直连 ba-docsearch 的 SQLite FTS5 库)。

自研大脑砍掉了 MCP 层,这里用 Python 标准库 sqlite3 只读打开 ba-docsearch 的
index.db(145k 页倍福官方文档,FTS5 全文索引),给 Agent 加"写代码前先查文档"
的能力——避免凭记忆写错库函数块 API(如 Tc2_TcpIp 的 FB_SocketAccept 成员名)。

不依赖 Python312 / 不起 subprocess:index.db 就是 SQLite,Python314 直接读。
库路径可用环境变量 BA_DOCS_DB 覆盖。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path


def _resolve_db() -> str:
    """按优先级定位文档索引库,方便打包:
    1) 环境变量 BA_DOCS_DB(部署时显式指定)
    2) 仓库内 data/ba-docs/index.db(随产品分发的标准位置,打包就打这个)
    3) 旧的外部 ba-docsearch 位置(开发机兜底)"""
    env = os.environ.get("BA_DOCS_DB")
    if env:
        return env
    bundled = Path(__file__).resolve().parent.parent / "data" / "ba-docs" / "index.db"
    if bundled.exists():
        return str(bundled)
    return r"G:\claude\Beckhoff Agent\ba-docsearch\index.db"


DB_PATH = _resolve_db()


def database_path() -> str:
    """Return the current index path.

    Keep ``DB_PATH`` for compatibility with callers that display it, but
    resolve the environment variable at call time so a packaged process can
    switch indexes before the first request without re-importing this module.
    """
    return _resolve_db()

# 只保留词元(字母数字下划线 + CJK),用空格连接 → FTS5 默认按 AND 匹配,
# 顺带规避 'FB_X.member' 里的点号等特殊字符导致的 FTS 语法错误。
_TOKEN = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_SPACE = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?。！？;；:：])\s+")

CONTEXT_READ_SUMMARY = 1800


def available() -> bool:
    return os.path.exists(database_path())


def _conn() -> sqlite3.Connection:
    path = database_path()
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _fts_query(q: str) -> str:
    toks = _TOKEN.findall(q or "")
    return " ".join(toks)


def _clean_text(value: object) -> str:
    """Clean index extraction artifacts and collapse whitespace for prompts."""
    text = str(value or "").replace("\ufffd", "").replace("��", "")
    return _SPACE.sub(" ", text).strip()


def _extract_summary(body: object, query: str = "", limit: int = CONTEXT_READ_SUMMARY) -> str:
    """Build a compact extractive summary without spending another model call.

    Query/API-related sentences are preferred, then restored to document order.
    This keeps exact member and parameter names, which is safer for PLC APIs than
    a free-form abstractive rewrite.
    """
    text = _clean_text(body)
    if len(text) <= limit:
        return text
    units = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if len(units) <= 1:
        return text[:limit].rstrip() + "…"

    terms = {term.lower() for term in _TOKEN.findall(query) if len(term) > 1}
    api_hints = (
        "var_input", "var_output", "input", "output", "parameter", "syntax",
        "function block", "method", "library", "bexecute", "bbusy", "berror",
        "nerrid", "ttimeout", "hsocket", "return", "example",
    )
    ranked: list[tuple[int, int, str]] = []
    for index, unit in enumerate(units):
        lower = unit.lower()
        score = sum(6 for term in terms if term in lower)
        score += sum(2 for hint in api_hints if hint in lower)
        if index < 3:
            score += 3 - index
        ranked.append((score, index, unit))

    selected: list[tuple[int, str]] = []
    used = 0
    for _score, index, unit in sorted(ranked, key=lambda item: (-item[0], item[1])):
        take = unit[: min(len(unit), 700)]
        if used + len(take) + 1 > limit and selected:
            continue
        selected.append((index, take))
        used += len(take) + 1
        if used >= limit * 0.9:
            break
    summary = " ".join(unit for _, unit in sorted(selected))[:limit].rstrip()
    return summary + "…"


def context_summary(
    tool_name: str,
    args: dict,
    result: object,
    state: dict | None = None,
) -> object:
    """Convert full document tool output into a small model-context summary.

    The full ``result`` is still sent to the UI. ``state`` is scoped to one
    agent turn and remembers search queries/read paths for relevance + dedupe.
    """
    if isinstance(result, dict) and "error" in result:
        return result
    if isinstance(result, dict) and result.get("summary_type"):
        return result  # 已经是持久化摘要，重复加载历史时保持原样
    state = state if state is not None else {}
    queries = state.setdefault("doc_queries", {})
    searches = state.setdefault("doc_searches", set())
    reads = state.setdefault("doc_reads", set())

    if tool_name == "docs_search" and isinstance(result, list):
        query = _clean_text(args.get("query", ""))
        query_key = "|".join(
            (
                query.casefold(),
                _clean_text(args.get("product", "")).casefold(),
                _clean_text(args.get("path_prefix", "")).casefold(),
            )
        )
        matches = []
        for item in result:
            if not isinstance(item, dict):
                continue
            path = _clean_text(item.get("path"))
            if path and query:
                queries[path] = query
            matches.append({
                "title": _clean_text(item.get("title")),
                "path": path,
                "product": _clean_text(item.get("product")),
                "key_excerpt": _clean_text(item.get("snippet")),
            })
        if query_key and query_key in searches:
            return {
                "summary_type": "beckhoff_docs_search_repeat",
                "query": query,
                "summary": "该关键词已搜索过，沿用前一次文档摘要。",
                "paths": [item["path"] for item in matches if item["path"]],
            }
        if query_key:
            searches.add(query_key)
        return {
            "summary_type": "beckhoff_docs_search",
            "query": query,
            "summary": f"命中 {len(result)} 篇；已对全部结果逐条生成检索摘要。",
            "matches": matches,
        }

    if tool_name == "docs_read" and isinstance(result, dict):
        path = _clean_text(result.get("path") or args.get("path"))
        title = _clean_text(result.get("title"))
        if path and path in reads:
            return {
                "summary_type": "beckhoff_doc_repeat",
                "title": title,
                "path": path,
                "summary": "该文档已读取，沿用前文摘要，不重复注入正文。",
            }
        if path:
            reads.add(path)
        query = queries.get(path, "")
        return {
            "summary_type": "beckhoff_doc",
            "title": title,
            "path": path,
            "related_query": query,
            "summary": _extract_summary(result.get("body", ""), query),
        }

    return result


def search(
    query: str,
    limit: int = 8,
    product: str | None = None,
    path_prefix: str | None = None,
) -> object:
    """全文搜倍福文档,按 FTS5 相关性返回命中列表。

    ``product`` 和 ``path_prefix`` 是可选的窄化条件，适合在同时命中
    多个 TwinCAT 产品或补充手册时减少误召回。BM25 对标题、产品、关键
    词和正文使用不同权重，避免结果依赖 SQLite 的 rowid 顺序。
    """
    if not available():
        return {"error": f"倍福文档库不可用({database_path()} 不存在)"}
    fq = _fts_query(query)
    if not fq:
        return {"error": "搜索词为空"}
    try:
        bounded_limit = max(1, min(int(limit or 8), 50))
    except (TypeError, ValueError):
        bounded_limit = 8
    conditions = ["docs_fts MATCH ?"]
    params: list[object] = [fq]
    if product:
        conditions.append("d.product LIKE ?")
        params.append(f"%{str(product).strip()}%")
    if path_prefix:
        prefix = str(path_prefix).strip().replace("\\", "/").lstrip("/")
        if prefix:
            conditions.append("d.path LIKE ?")
            params.append(prefix.rstrip("/") + "%")
    params.append(bounded_limit)
    where = " AND ".join(conditions)
    try:
        c = _conn()
        rows = c.execute(
            "SELECT d.title, d.path, d.product, "
            "snippet(docs_fts, -1, '《', '》', '…', 16) "
            "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
            f"WHERE {where} "
            "ORDER BY bm25(docs_fts, 8.0, 4.0, 5.0, 2.0, 3.0, 1.0) ASC, d.id ASC "
            "LIMIT ?", params).fetchall()
        return [{"title": r[0], "path": r[1], "product": r[2], "snippet": r[3]}
                for r in rows]
    except Exception as e:  # noqa: BLE001
        return {"error": f"搜索失败: {e}"}
    finally:
        try:
            c.close()
        except Exception:
            pass


def read(path: str, limit: int = 8000) -> object:
    """按 path 读取某篇文档的正文(用于看清库函数块的完整 API)。超长截断。"""
    if not available():
        return {"error": f"倍福文档库不可用({database_path()} 不存在)"}
    try:
        c = _conn()
        row = c.execute(
            "SELECT d.title, d.path, docs_fts.body "
            "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
            "WHERE d.path = ? LIMIT 1", (path,)).fetchone()
        if not row:
            return {"error": f"未找到文档: {path}"}
        body = (row[2] or "")
        truncated = len(body) > limit
        return {"title": row[0], "path": row[1],
                "body": body[:limit] + ("…(正文过长已截断)" if truncated else "")}
    except Exception as e:  # noqa: BLE001
        return {"error": f"读取失败: {e}"}
    finally:
        try:
            c.close()
        except Exception:
            pass
