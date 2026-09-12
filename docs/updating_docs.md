# 更新倍福文档库（docs_search 用的索引）

TwinCAT Agent 的 `docs_search` / `docs_read` 工具查的是一个 **SQLite FTS5 全文索引**
`data/ba-docs/index.db`（约 374MB，倍福 InfoSys ~145k 页文档的正文 + 索引）。
产品运行时**只需要这一个 `index.db`**（正文都在库里，不需要原始 HTML）。

精选知识源（仓库内 Markdown/PDF）的登记和可信边界见
[`knowledge_base/catalog.yaml`](../knowledge_base/catalog.yaml)，修改后先运行：

```powershell
python scripts/validate_knowledge_base.py
```

## 位置约定

`tc_agent/docsearch.py` 按这个优先级找库：
1. 环境变量 `BA_DOCS_DB`（部署时显式指定，最高优先）
2. 仓库内 `data/ba-docs/index.db` ← **标准位置，打包就打它**
3. 旧的外部 `G:\claude\Beckhoff Agent\ba-docsearch\index.db`（开发机兜底）

`data/ba-docs/` 已在 `.gitignore`（大二进制不入 git，随安装包分发）。

## 什么时候要更新

倍福发布了新版 InfoSys / 你补了新文档，想让 `docs_search` 能搜到时。

## 怎么更新（三步）

索引的**源头**是 ba-docsearch 工具（`G:\claude\Beckhoff Agent\ba-docsearch`），
它有一个原始文档目录 `BA_DOCS_ROOT`（默认 `G:\claude\Beckhoff Agent\Knowledge base`）
和重建索引的 CLI。流程：

1. **更新原始文档**：把新的倍福 HTML 文档放进 `Knowledge base` 目录
   （或用 ba-docsearch 自己的抓取脚本更新）。

2. **重建索引**（ba-docsearch 必须用 **Python312**）：
   ```powershell
   $env:BA_DOCS_ROOT = "G:\claude\Beckhoff Agent\Knowledge base"
   $env:BA_DOCS_DB   = "G:\claude\Beckhoff Agent\ba-docsearch\index.db"
   cd "G:\claude\Beckhoff Agent\ba-docsearch"
   & "C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python312\python.exe" -m ba_docsearch.cli index          # 增量：只处理新增/改动
   # 或 --rebuild 全量重建 / --prune 顺带删掉已删文件的行
   ```

3. **把新库同步到产品位置**（一条命令，或直接跑下面的脚本）：
   ```powershell
   Copy-Item "G:\claude\Beckhoff Agent\ba-docsearch\index.db" `
             "G:\claude\twin-cat-agent\data\ba-docs\index.db" -Force
   ```

完成后重启后端（或让它下次启动时加载），`docs_search` 就能搜到新内容了。

更新完成后建议做一次只读完整校验：

```powershell
python scripts/validate_knowledge_base.py --check-index
```

## 一键脚本

上面第 2、3 步已封装成 `scripts/update_docs.ps1`，直接运行即可（普通 PowerShell）：
```powershell
& "G:\claude\twin-cat-agent\scripts\update_docs.ps1"
```

脚本还会从本机官方 CoAgent 文档目录选择性补齐 TE1200、PJLink、TF3550、
TF4500 和部分 TF52xx/CNC 手册。官方源文件是 Markdown；脚本会转换为只供
索引器读取的 HTML，再统一写入同一个 `index.db`。不会复制完整 CoAgent 文档包。

仓库内 `knowledge_base/source/TF55xx_TC3_MC3_EN.pdf` 会由
`scripts/import_pdf_manual.py` 按页转换为可检索 HTML，再写入同一索引。
因此发布包仍只需要 `index.db`，不需要额外携带 353 页的原始 PDF。

若当前机器未安装官方 CoAgent，脚本会给出警告并继续更新已有 InfoSys 镜像；
如需明确跳过该步骤，可使用：

```powershell
& "G:\claude\twin-cat-agent\scripts\update_docs.ps1" -SkipOfficialSupplement
```

## 打包时

安装包把 `data/ba-docs/index.db` 一并打进去，装到客户机的对应位置即可；
或者让安装包设置 `BA_DOCS_DB` 环境变量指向它放的地方。客户机**不需要** Python312、
不需要 ba-docsearch 工具、不需要原始 HTML —— 只要这个 `index.db` 和产品自带的
`tc_agent/docsearch.py`（纯标准库 sqlite3）。
