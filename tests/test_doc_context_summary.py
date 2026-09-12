from __future__ import annotations

import json
import unittest

from tc_agent import agent_core, docsearch


class DocContextSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.results = [
            {
                "title": f"FB_SocketClose page {index}",
                "path": f"content/socket/{index}.html",
                "product": "TS6xxx - Communication",
                "snippet": "��FB_SocketClose closes a socket. bExecute starts the block. "
                + ("parameter details " * 40),
            }
            for index in range(8)
        ]

    def test_search_context_summarizes_every_result_without_extra_limits(self) -> None:
        state: dict = {}
        summary = docsearch.context_summary(
            "docs_search", {"query": "FB_SocketClose"}, self.results, state
        )
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertEqual(summary["summary_type"], "beckhoff_docs_search")
        self.assertEqual(len(summary["matches"]), len(self.results))
        self.assertEqual(summary["matches"][0]["path"], "content/socket/0.html")
        self.assertEqual(
            summary["matches"][0]["key_excerpt"],
            docsearch._clean_text(self.results[0]["snippet"]),
        )
        self.assertNotIn("�", encoded)

    def test_read_context_is_query_focused_and_deduplicated(self) -> None:
        state: dict = {}
        docsearch.context_summary(
            "docs_search", {"query": "FB_SocketClose bExecute"}, self.results, state
        )
        body = (
            "General TCP introduction. " * 300
            + "FB_SocketClose input bExecute starts the function block. "
            + "VAR_OUTPUT bBusy bError nErrId report execution state. "
            + "Additional unrelated information. " * 100
        )
        result = {
            "title": "FB_SocketClose",
            "path": "content/socket/0.html",
            "body": body,
        }
        first = docsearch.context_summary(
            "docs_read", {"path": result["path"]}, result, state
        )
        second = docsearch.context_summary(
            "docs_read", {"path": result["path"]}, result, state
        )
        self.assertLessEqual(len(first["summary"]), docsearch.CONTEXT_READ_SUMMARY + 1)
        self.assertIn("FB_SocketClose", first["summary"])
        self.assertEqual(second["summary_type"], "beckhoff_doc_repeat")

    def test_compaction_migrates_old_document_body_once(self) -> None:
        messages = [
            {
                "role": "tool",
                "id": "1",
                "name": "docs_read",
                "result": {
                    "title": "API",
                    "path": "content/api.html",
                    "body": "input output parameter. " * 1000,
                },
            }
        ]
        compacted, saved = agent_core.compact_messages(messages)
        self.assertGreater(saved, 1000)
        self.assertEqual(compacted[0]["result"]["summary_type"], "beckhoff_doc")
        again, saved_again = agent_core.compact_messages(compacted)
        self.assertEqual(again[0]["result"], compacted[0]["result"])
        self.assertEqual(saved_again, 0)


if __name__ == "__main__":
    unittest.main()
