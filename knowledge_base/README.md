# TwinCAT Agent 知识库

本目录是 Agent 的“精选知识层”，用于保存可审阅、可追溯、适合注入上下文的内容；它不是把所有官方资料复制一份。完整 Beckhoff InfoSys 文档由 `data/ba-docs/index.db` 提供 `docs_search/docs_read` 检索。

## 两层知识架构

| 层 | 内容 | 使用方式 | 可信边界 |
|---|---|---|---|
| 精选层 | 本目录的 Markdown、原始 PDF 和 `catalog.yaml` | 设计工作流、生成上下文、沉淀本项目验证过的约束 | 必须遵守 `catalog.yaml` 的 authority/kind；版本敏感内容仍需回查官方文档 |
| 官方检索层 | `data/ba-docs/index.db`，当前约 145,407 篇 | `docs_search` 找入口，`docs_read` 读取正文 | 以实际安装的 TwinCAT/TE2000 版本和目标手册为准 |

知识源登记在 [`catalog.yaml`](catalog.yaml) 中。每个源必须声明来源、语言、主题、权威级别、状态和验证边界；没有登记的 Markdown/PDF 不应作为正式知识源交付。

## Agent 检索规则

1. 需要 Beckhoff 库、FB、方法或参数时，先 `docs_search`，再按命中结果调用 `docs_read`。
2. 使用精选 Markdown 了解整体结构和仓库约束；使用官方正文确认精确 API、版本差异、许可和安全要求。
3. HMI、运行时、Safety、部署等有破坏性或版本敏感行为时，优先读取当前工程/目标状态，不能仅凭知识库推断已生效。
4. `operational-runbook` 只约束本项目 Agent 的工作方式，不应被引用为 TwinCAT 官方事实。
5. 当来源之间冲突时，优先级为：实际 XAE/Runtime 回读 > 匹配版本的 Beckhoff 官方文档 > `implementation-contract` > 其他精选摘要 > 培训摘要。

## 新增或更新知识的流程

```powershell
# 1. 编辑/导入内容，并在 catalog.yaml 登记来源与边界
# 2. 校验精选层
python scripts/validate_knowledge_base.py

# 3. 更新官方 FTS5 索引（需要本机 ba-docsearch 与 Python 3.12）
& scripts/update_docs.ps1

# 4. 连同索引做完整校验
python scripts/validate_knowledge_base.py --check-index
```

PDF 通过 `scripts/import_pdf_manual.py` 按页转成临时 HTML，再由 `update_docs.ps1` 写入同一个 FTS5 索引；产品运行时只需要 `index.db`，不需要携带中间 HTML。

## 交付前检查清单

- [ ] 文件已登记到 `catalog.yaml`，且路径在仓库内。
- [ ] 明确区分官方原文、官方摘要、仓库验证契约和内部经验。
- [ ] 版本、语言、页码、哈希或外部来源信息完整。
- [ ] 结论包含验证边界，不把静态配置说成在线事实。
- [ ] 运行 `python scripts/validate_knowledge_base.py` 通过。
- [ ] 若影响官方检索，重建后运行 `--check-index` 并用代表性 API 关键词抽查。
