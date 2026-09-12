---
name: docsearch-integration
description: coAgent 的倍福文档搜索(docs_search/docs_read)——直连 SQLite FTS5、打包位置、更新流程
metadata:
  type: reference
---

# coAgent 文档搜索(docs_search / docs_read)集成

## 是什么 / 为什么
自研大脑(agent_core)砍掉 MCP 层时,曾漏掉倍福文档检索,导致 Agent 凭记忆写库
函数块代码、把 Tc2_TcpIp 的 socket API 全写错(FB_SocketAccept.hNewSocket 不存在等,
一次编译 42 错)。补回方式:**Python314 标准库 sqlite3 直连 ba-docsearch 的 FTS5 库**,
不走 MCP、不依赖 Python312、不起 subprocess。0.01s/查,145k 页倍福 InfoSys。

- 模块 `tc_agent/docsearch.py`:`search(query,limit)` / `read(path)`,只读打开 index.db。
  FTS 查询先把词元(字母数字下划线+CJK)抽出空格连接,规避 'FB_X.member' 点号导致的
  FTS 语法错误。
- 工具 `docs_search` / `docs_read` 注册在 agent_core.REGISTRY(只读,自动放行)。
- 系统提示(backend `_system_prompt`)已引导:写/改用到库函数块(Tc2_TcpIp/Tc2_Standard/
  运动 FB 等)的代码前,先 docs_search 查官方文档确认 API,再动手。实测会自主
  docs_search→docs_read 多次查清再写。

## DB schema(index.db,~374MB,SQLite FTS5)
- 表 `docs`(元数据):id, path, doc_id, title, product, category, productgroup,
  keywords, description, parent, level, breadcrumb, mtime, size
- FTS 表 `docs_fts`(可搜):title, product, keywords, description, breadcrumb, body
- 关联:`docs.rowid = docs_fts.rowid`;正文在 `docs_fts.body`(所以产品运行时**只需
  index.db,不需要原始 HTML**)。

## 打包位置(优先级,见 docsearch._resolve_db)
1. 环境变量 `BA_DOCS_DB`(部署显式指定)
2. **`<repo>/data/ba-docs/index.db`** ← 标准位置,打包就打它(已 gitignore,374MB 大二进制)
3. `G:\claude\Beckhoff Agent\ba-docsearch\index.db`(开发机兜底旧位置)

产品打包:安装包带上 `data/ba-docs/index.db` 即可;客户机不需要 Python312 / ba-docsearch /
原始 HTML,只要该库 + docsearch.py(纯 sqlite3)。

## 更新流程(索引源头在 ba-docsearch 工具,必须 Python312)
详见 `docs/updating_docs.md`。三步:① 补文档到 `G:\claude\Beckhoff Agent\Knowledge base`
② `python312 -m ba_docsearch.cli index`(增量;--rebuild 全量;--prune 清删)重建
`ba-docsearch\index.db` ③ 拷到 `<repo>/data/ba-docs/index.db`。
已封装 **`scripts/update_docs.ps1`**(一键重建+同步,须 UTF-8 BOM 否则 PS5.1 中文串报错)。
跑完重启后端生效。相关:[[coagent-backend-operations]]、[[ba-docsearch-tool]](用户级记忆)。
