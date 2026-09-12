from __future__ import annotations

import sqlite3
from pathlib import Path

from tc_agent import docsearch
from scripts.validate_knowledge_base import validate_catalog


def _create_index(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE docs (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            doc_id TEXT,
            title TEXT,
            product TEXT,
            category TEXT,
            productgroup TEXT,
            keywords TEXT,
            description TEXT,
            parent TEXT,
            level TEXT,
            breadcrumb TEXT,
            mtime REAL,
            size INTEGER
        );
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            title, product, keywords, description, breadcrumb, body
        );
        """
    )
    rows = [
        (1, "content/other/slow.html", "MC_Stop", "TF5xxx - Motion", "MC_Stop axis stop"),
        (2, "content/other/exact.html", "MC_Stop", "TF5xxx - Motion", "MC_Stop MC_Stop Execute Done"),
        (3, "content/agent/mc3.html", "MC_Stop", "TF55xx TwinCAT 3 MC3", "MC_Stop Axis Execute Done"),
    ]
    for row_id, path, title, product, body in rows:
        connection.execute(
            "INSERT INTO docs(id, path, title, product, keywords) VALUES (?, ?, ?, ?, ?)",
            (row_id, path, title, product, body),
        )
        connection.execute(
            "INSERT INTO docs_fts(rowid, title, product, keywords, description, breadcrumb, body) "
            "VALUES (?, ?, ?, ?, '', '', ?)",
            (row_id, title, product, body, body),
        )
    connection.commit()
    connection.close()


def test_curated_catalog_covers_all_sources() -> None:
    assert validate_catalog() == []


def test_docsearch_resolves_runtime_db_and_applies_relevance_filters(tmp_path, monkeypatch) -> None:
    db = tmp_path / "index.db"
    _create_index(db)
    monkeypatch.setenv("BA_DOCS_DB", str(db))

    results = docsearch.search("MC_Stop", limit=10, product="TF55xx")
    assert [item["path"] for item in results] == ["content/agent/mc3.html"]

    results = docsearch.search(
        "MC_Stop", limit=10, path_prefix="content/other/"
    )
    assert results[0]["path"] == "content/other/exact.html"
    assert len(results) == 2


def test_docsearch_limit_is_bounded_and_empty_queries_are_rejected(tmp_path, monkeypatch) -> None:
    db = tmp_path / "index.db"
    _create_index(db)
    monkeypatch.setenv("BA_DOCS_DB", str(db))

    assert docsearch.search("MC_Stop", limit=500) and len(docsearch.search("MC_Stop", limit=500)) <= 50
    assert docsearch.search("...") == {"error": "搜索词为空"}


def test_doc_context_does_not_dedupe_different_scopes() -> None:
    state: dict = {}
    first = docsearch.context_summary(
        "docs_search",
        {"query": "MC_Stop", "product": "TF5xxx"},
        [{"title": "TF5", "path": "tf5.html", "product": "TF5xxx", "snippet": "stop"}],
        state,
    )
    second = docsearch.context_summary(
        "docs_search",
        {"query": "MC_Stop", "path_prefix": "content/agent/"},
        [{"title": "MC3", "path": "mc3.html", "product": "TF55xx", "snippet": "stop"}],
        state,
    )
    assert first["summary_type"] == "beckhoff_docs_search"
    assert second["summary_type"] == "beckhoff_docs_search"
