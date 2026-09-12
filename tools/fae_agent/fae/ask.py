from __future__ import annotations

import argparse
import json
import sys

from .retrieve import DB, ROOT, read_source, rebuild, search


SYSTEM_PROMPT = """你是倍福中国技术支持工程师。先给结论，再给依据和排查步骤。
禁止编造型号、参数和错误码；价格、库存和交期转商务；安全问题只给技术事实并要求按标准评估。
区分官方文档结论与经验建议。中文虚拟学院未检索到时必须明确说明，并继续查 InfoSys 官方资料。
回复保持简短，型号、功能块和界面名称保留英文原文。"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m fae.ask")
    sub = parser.add_subparsers(dest="command", required=True)
    ask_parser = sub.add_parser("ask")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--limit", type=int, default=6)
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    index_parser = sub.add_parser("index")
    index_parser.add_argument("--rebuild", action="store_true")
    index_parser.add_argument("--prune", action="store_true")
    sub.add_parser("stats")
    read_parser = sub.add_parser("read")
    read_parser.add_argument("locator")
    args = parser.parse_args()

    if args.command == "index":
        print(json.dumps(rebuild(prune=args.prune), ensure_ascii=False, indent=2))
    elif args.command == "stats":
        result = {"corpus": str(ROOT), "corpus_exists": ROOT.exists(), "index": str(DB), "index_exists": DB.exists()}
        if DB.exists():
            import sqlite3
            connection = sqlite3.connect(DB)
            result["chunks"] = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            result["files"] = connection.execute("SELECT count(DISTINCT locator) FROM chunks").fetchone()[0]
            connection.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "read":
        print(read_source(args.locator))
    else:
        query = args.question if args.command == "ask" else args.query
        results = search(query, args.limit)
        if args.command == "ask":
            print("=== SYSTEM PROMPT ===\n" + SYSTEM_PROMPT + "\n\n=== 证据包 ===")
        if not results:
            print("中文虚拟学院未检索到相关内容。")
        for index, item in enumerate(results, 1):
            print(f"[CN{index}] {item['title']}\n{item['excerpt']}\nSource: {item['locator']}\n")


if __name__ == "__main__":
    main()
