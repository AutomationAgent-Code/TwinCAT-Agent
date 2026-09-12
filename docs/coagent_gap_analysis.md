# TwinCAT Agent vs Beckhoff coAgent — 能力对标与查漏补缺

> 对标日期：2026-07-18
> 参考：Beckhoff 官方 **coAgent** 工具清单（sysman-mcp 42 + PLC Libraries 3 + ba-filesearch 4 + Buildin Code Tools 14）
> 本仓库：`tc-template` CLI（template / plc / tc / diag 命令组）

## 0. 说明

- coAgent 的 **Buildin Code Tools（14）**（read_file / write_code / grep_code / list_files…）是通用文件/代码工具。本 Agent 运行在 Claude Code 上，这些是**内置能力**（Read/Write/Edit/Grep/Glob），不算缺口。
- 真正对比的是 TwinCAT 专有工具：**sysman-mcp（42）**、**PLC Libraries（3）**、**ba-filesearch-mcp（4，文档检索）**。
- 状态图例：✅ 已覆盖 · 🟡 半缺/部分 · 🔴 缺失 · ⚪ 设计差异（不一定要补）

## 1. sysman-mcp（42）逐项映射

| Beckhoff 工具 | 本 Agent 对应 | 状态 |
|---|---|---|
| activate | `tc activate` | ✅ |
| deploy_to_target | `tc deploy` | ✅ |
| restart_target | `tc restart`（独立） | ✅ 已补 |
| scan_io_hardware | `tc scan` | ✅ |
| add_io_devices | `tc scan`（合并在扫描流程） | ✅ |
| close_project | `tc close` | ✅ |
| set_target | `tc target set` | ✅ |
| list_targets | `tc target list` | ✅ |
| get_system_status | `tc state` / `diag health` | ✅ |
| get_target_state | `tc state` | ✅ |
| get_mappings | `tc link show` | ✅ |
| map_variables | `tc link add` | ✅ |
| plc_build | `plc build` / `tc build` | ✅ |
| plc_online | `tc online` | ✅ |
| plc_read_code | `plc read` | ✅ |
| plc_write_code | `plc write`（COM） | ✅ |
| plc_create_pou | `plc create-com` | ✅ |
| plc_add_library | `plc lib-add` | ✅ |
| plc_remove_library | `plc lib-remove` | ✅ |
| plc_errorlist | `tc inspect` | ✅ |
| **create_plc_project** | `plc create-project [name]` | ✅ 已补（2026-07-18） |
| **delete_plc_project** | `plc delete-project <name>` | ✅ 已补 |
| **plc_delete_pou** | `plc delete-pou <name>` | ✅ 已补 |
| **plc_structure** | `plc structure` | ✅ 已补 |
| **plc_search** | `plc search <pattern> [--regex] [--case]` | ✅ 已补 |
| **get_plc_variables** | `plc vars [pou]` | ✅ 已补 |
| **get_io_structure** | `tc io-structure [--depth N]` | ✅ 已补 |
| **get_ethercat_master_network_adapter** | `tc ethercat-adapter` | ✅ 已补 |
| **change_ethercat_master_network_adapter** | `tc ethercat-adapter-set <dev> <desc>` | ✅ 已补（best-effort，需硬件校验） |
| **read_device_settings** | `tc device-read <path>` | ✅ 已补 |
| **write_device_settings** | `tc device-write <path> <xml>` | ✅ 已补 |
| **rename_elements** | `plc rename <old> <new>` | ✅ 已补 |
| **list_projects** | `tc projects` | ✅ 已补 |
| **get_project_info** | `tc project-info` | ✅ 已补 |
| restart_target（独立） | `tc restart` | ✅ 已补 |
| delete_treeitem | ❌ 无（通用树删除） | ⚪ 设计差异 |
| get_treeitems / get_treeiteminfo / set_treeiteminfo | ❌ 无（通用树读写 API） | ⚪ 设计差异 |
| context_menu / execute_context_menu_item | ❌ 无（通用右键菜单） | ⚪ 设计差异 |
| add_new_item_init / add_new_item_execute | 部分：`plc create-com` 覆盖建对象 | ⚪ 设计差异 |
| delete_treeitem | ❌ 无 | ⚪ 设计差异 |

## 2. PLC Libraries（3）

| Beckhoff | 本 Agent | 状态 |
|---|---|---|
| list_plc_libraries | `plc libraries` | ✅ |
| get_plc_library_info | 部分：`plc lib-scan` | 🟡 |
| **search_plc_pous** | ❌ 无（跨库搜 POU） | 🟡 缺 |

## 3. ba-filesearch-mcp（4）— 文档检索

`get_doc_index / grep_search / read_section / search_docs` → ✅ **已补（2026-07-18）**：`G:\claude\Beckhoff Agent\ba-docsearch`（SQLite FTS5 索引 145k 页 InfoSys 镜像）。MCP 工具 `search_docs / read_doc / list_products / grep_docs / index_stats` + CLI（`index` 增量更新）。对应关系：search_docs→search_docs、grep_search→grep_docs、read_section→read_doc、get_doc_index→list_products/index_stats。

## 4. 优先级建议

### ✅ P0 高价值缺口 — 已全部实现（2026-07-18）
1. **create_plc_project** → `plc create-project [name]`（`TIPC.CreateChild(name,0,"","Standard PLC Template")`）
2. **plc_delete_pou** → `plc delete-pou <name>`（`folder.DeleteChild(name)`）
3. **get_plc_variables** → `plc vars [pou]`（解析声明区 VAR 块 → name : type · scope）
4. **plc_structure** → `plc structure`（遍历 POUs/DUTs/GVLs/VISUs + 方法）
5. **plc_search** → `plc search <pattern> [--regex] [--case]`（全项目声明+实现+方法 grep）

外加 **delete_plc_project** → `plc delete-project <name>`。

**顺带修复的真 bug**：
- `_com_dte`/`_dte`/`_get_dte` 现在也尝试 `TcXaeShell.DTE.17.0`（4026 版 ProgID，之前只试 15.0/14.0 → 连不上）。
- `plc create-com` 的 `-t fb` / `-t visu` 短别名现在可用；vInfo 改由 `sub_type` 判定（POU→语言串 / DUT→`''` / GVL·VISU·Interface→None），修正了别名与 DUT 创建。

### ✅ P1 中价值 — 已实现（2026-07-18）
- `rename_elements` → `plc rename <old> <new>`（`item.Name = new`）
- `list_projects` → `tc projects`、`get_project_info` → `tc project-info`
- `get_io_structure` → `tc io-structure [--depth N]`（遍历 TIID 树）
- `read_device_settings` → `tc device-read <path>`、`write_device_settings` → `tc device-write <path> <xml>`（ProduceXml/ConsumeXml）
- `get_ethercat_master_network_adapter` → `tc ethercat-adapter`、`change_…` → `tc ethercat-adapter-set <dev> <desc>`（best-effort，需实机校验 NIC 描述符格式）
- `restart_target` → `tc restart`（独立重启，复用 `restart_twincat()`）

> `restart` / `device-write` / `ethercat-adapter-set` 为有副作用/需硬件的命令，已接线并复用已验证底层，但未在无 I/O 的测试项目上实机执行。

### 🟡 仍未做（低优先/依赖库枚举）
- `search_plc_pous` / `get_plc_library_info` — 需要枚举**引用库内部**的 POU，COM 库管理器 API 暴露有限、且无实机可测，暂缓。项目自身 POU 检索已由 `plc search` / `plc structure` 覆盖。

### ⚪ 设计差异（可选，不一定要抄）
coAgent 暴露了一套**通用 COM 树 API**（get/set_treeitem、context_menu、add_new_item）。本 Agent 走"高层封装命令"路线，更安全好用但不如通用树灵活。若要，可加一个薄 `tc tree` 组封装 `LookupTreeItem` / `ChildCount` / `Child(i)` / `ProduceXml` / `ConsumeXml`。

## 5. 本 Agent 领先 coAgent 的地方

| 能力 | 说明 | coAgent |
|---|---|---|
| **模板系统** | 14 模板，create / extract / validate / add | ❌ 完全没有 |
| **设备发现 + 路由注册** | `target search` / `find` / `add --auto-auth` | ❌ 只有 set/list |
| **版本固定** | `version pin` / `unpin` | ❌ |
| **构建平台自动检测** | TIRS CPUType → x64/ARM 自动选平台 | ❌ |
| **NC 轴自动配置** | `tc ncConfigurate`（建 NC 任务+轴+链接伺服） | ❌ |
| **诊断组** | `diag health/ping/route/target/errors/license` | 部分（get_system_status） |
| **库仓库管理** | `repo-add/remove`、`lib-install/uninstall`、`placeholder-*` | 部分（仅 add/remove library） |

## 6. 结论

本 Agent 在 **PLC 树操作的细粒度**（建/删项目、删 POU、符号列举、结构、搜索）上落后 coAgent；但在 **工程化/自动化流程**（模板、设备发现、路由、版本、平台、NC、部署、诊断）上明显领先。

补齐 P0 五项后，日常 PLC 编程闭环即可对齐 coAgent 的核心体验。
