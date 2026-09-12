# TwinCAT Agent 自动化工作流

本文是仓库级操作手册，不是 Beckhoff 官方产品手册。工具行为、授权边界和当前版本差异以仓库中的 `AGENTS.md`、工具描述、实际 XAE/Runtime 回读以及匹配版本的官方文档为准。

## 1. 知识检索优先级

涉及 Beckhoff 库函数块、方法、参数、版本差异或错误码时：

1. 先用 `docs_search` 搜官方 InfoSys。
2. 对候选结果用 `docs_read` 读取正文，确认输入/输出/方法名和限制条件。
3. 再结合 `knowledge_base/` 中的精选摘要和当前项目代码生成方案。
4. 代码写入后编译并回读；不能把“文档命中”当作“当前目标已验证”。

## 2. 项目与 PLC 工作流

### 创建项目

```text
tc-template create <template> -n <name> -o <dir>
tc platform set
```

模板选择先查看模板库；运动控制优先检查运动模板，标准机器优先检查 PackML/SPT 模板。创建后确认实际解决方案、PLC 项目和目标信息，不因用户提及设备 IP 就自动切换目标。

### 编写 PLC

```text
fblib find "<功能意图>"
```

有匹配的已验证功能块时优先 `fblib add`，再按项目需求修改；没有匹配时按 `docs/plc_coding_standard.md` 生成。编写任何 FB、POU、DUT 或 GVL 前都要遵循声明分区、中文头注释、状态四件套、`CASE eState` 状态机和子 FB 调用顺序要求。

代码编辑优先使用 COM 读写接口；只有离线或紧急场景才使用文件系统 XML。写入后读取同一对象确认声明/实现已经被 XAE 重解析。

### 编译和上线

```text
tc build
```

仅修改 POU/GVL 且不涉及 I/O 映射、地址分配或 NC 配置时，获得明确上线授权后才走 `tc online`。涉及 I/O、NC 或配置变更时，获得明确授权后走完整部署流程；部署不隐式扫描硬件、切目标、切 Config、激活或重启。

## 3. 结果可信度

- `docs_search`/`docs_read` 证明的是官方文档中存在相关说明。
- `plc build` 证明的是当前 PLC 工程能否编译。
- `tc activate` 只提交配置；实际生效还需要按场景重启并回读状态。
- 在线变量、HMI ADS 符号、任务运行指标和 Safety 行为必须使用对应的在线/结构读取工具验证。
- 任何 Safety 写操作仍需人工审查、风险评估、Verify 和现场验收。

## 4. 知识库维护

新增或修订资料时，先在 `knowledge_base/catalog.yaml` 登记来源、权威级别、主题和验证边界，再运行：

```powershell
python scripts/validate_knowledge_base.py
```

如果内容需要进入官方全文检索索引，按 [`docs/updating_docs.md`](../docs/updating_docs.md) 更新 `data/ba-docs/index.db`，最后运行：

```powershell
python scripts/validate_knowledge_base.py --check-index
```
