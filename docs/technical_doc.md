# TwinCAT Agent — 技术文档

## 技术架构

```
                    ┌──────────────────────────────────┐
                    │       Claude Code (AI Agent)       │
                    │    /twincat-template  /plc  /tc    │
                    └──────────────┬───────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
    ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
    │   文件系统 (XML)  │  │  COM Automation │  │   pyautogui     │
    │  .TcPOU .TcDUT  │  │  win32com.DTE   │  │  Ctrl+A/C/V/S   │
    │  .TcGVL .plcproj│  │  ITcSysManager  │  │  Mouse Click    │
    └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  ▼
                       ┌──────────────────┐
                       │  TwinCAT 3 XAE   │
                       │  (TcXaeShell)    │
                       └──────────────────┘
```

## 项目统计

| 指标 | 数值 |
|------|------|
| Python 模块 | 13 个文件, 3,763 行 |
| CLI 命令 | 3 个命令组, 30+ 命令 |
| 模板 | 14 个 (packml + model1 + 12 SPT) |
| Skill 文件 | 3 个 |
| 精选知识库 | 5 个 Markdown 源 + 1 个原始 PDF，统一登记在 `knowledge_base/catalog.yaml` |
| 官方文档检索 | SQLite FTS5，当前约 145,407 篇 InfoSys 文档 |

## 模块详述

### 模板管理 (`tc_template/scaffold.py` + `extract.py` + `guid_utils.py`)

**GUID 保护策略**

提取模板时, 项目特定 GUID 被替换为 `{{GUID_N}}` 占位符, Beckhoff 系统 GUID 保留原值。

| 替换 (白名单) | 保留 (不可动) |
|---|---|
| `Id="..."` 属性 | `{00000000-...}` null GUID (哨兵) |
| `ProjectGUID="..."` / `ProjectGuid="..."` | `{18071995-*}` Beckhoff Type System |
| `<Application>` `<TypeSystem>` `<LibraryReferences>` | `{B1E792BE-...}` TcXaeShell |
| `GuidA="..."` `GuidB="..."` `TmcHash="..."` | `{08500001-...}` TcPlc30 CLSID |
| `FbGuid`, `OptionKey` GUID | `<ProjectExtensions>` 段所有 GUID |
| | `<PlaceholderReference>` `<Licenses>` `<Device>` 段 |

**扫描机制**: 白名单属性匹配 (非全文件正则扫描), 避免误抓 MC 类型系统 Name GUID= 和 Type GUID=。

**`_Libraries`**: 提取和创建阶段均原样复制, 0 处理。

### PLC 编程 (`tc_template/plc.py`)

**COM 优先读写 — ITcPlcDeclaration / ITcPlcImplementation**

所有 PLC 代码读写现在通过 COM 接口完成，不再使用文件系统 XML CDATA 解析。

| 操作 | COM (新) | 文件系统 (旧) | pyautogui (旧) |
|------|---------|-------------|---------------|
| List 对象 | ✅ `com_list_objects()` | ✅ | ❌ |
| Read 代码 | ✅ **`com_read_pou()`** | ✅ XML CDATA | ✅ |
| Write 代码 | ✅ **`com_write_pou()`** | ✅ CDATA marker | ✅ |
| Create POU | ✅ CreateChild + 代码写入 | ❌ | ⚠️ |
| 是否弹窗 | ❌ **IDE 内部操作** | ❌ 触发 File Modified 对话框 | ❌ |

**COM 接口:**

| Interface | Property | 方向 |
|-----------|----------|------|
| `ITcPlcDeclaration` | `DeclarationText` | 读 / 写 |
| `ITcPlcImplementation` | `ImplementationText` | 读 / 写 |

通过 `win32com.client.CastTo(pou_item, "ITcPlc...")` 从 COM 树节点获取。

**核心优势:** COM 写入是 IDE 内部操作 — TwinCAT 自动重新解析，**不弹 "File Modified" 对话框**。无需 `_tree_to_string()` CDATA marker 序列化，无需 pyautogui Ctrl+S reload。

**COM CreateChild 参考**

| 对象 | SubType | vInfo |
|------|---------|-------|
| Function Block | 604 | `[str(lang_id)]` |
| Program | 603 | `[str(lang_id)]` |
| Struct | 606 | `None` |
| Enum | 605 | `None` |
| GVL | 615 | `None` |
| Visu | 619 | `None` |

**关键教训**: `vInfo='ST'` (字符串) 创建 ST 实现, `vInfo=['6']` (数组) 会错误生成 NWL 梯形图。

### 平台控制 (`tc_template/tc_platform.py`)

**Deploy 流水线**

```
build → activate → restart → dismiss dialogs → login → start
  │        │          │           │              │        │
  │        │          │   pyautogui 后台线程      │        │
  │        │          │   每秒点击屏幕中央 OK     │        │
  │        │          └──────────────────────────┘        │
  │        │                                              │
  │   ActivateConfiguration()              LoginCmd + StartCmd
  │   "Save To Registry"                  ConsumeXml on PLC Instance
  │
  SolutionBuild.Build()
  EnvDTE.DTE
```

**弹窗处理方案**

TwinCAT XAE 在激活和重启时会弹出确认对话框。不同版本的 XAE 弹窗位置略有不同, 因此采用多位置点击策略:

```python
# 屏幕中央 + 多偏移量点击, 覆盖不同弹窗布局
for (x_off, y_off) in [(0, 30), (0, 60), (60, 60), (-60, 60), (0, 90)]:
    pyautogui.click(sw//2 + x_off, sh//2 + y_off)
```

配合 `TcAutomationSettings.SilentMode = True` 抑制大部分弹窗, 剩余由点击线程处理。

**PLC 在线命令**

在线操作 (Login/Logout/Start/Stop) 通过 ConsumeXml 发送到 PLC Instance 节点:

```xml
<TreeItem>
  <IECProjectDef>
    <OnlineSettings>
      <Commands>
        <LoginCmd>true</LoginCmd>
        <StartCmd>true</StartCmd>
      </Commands>
    </OnlineSettings>
  </IECProjectDef>
</TreeItem>
```

### 错误列表读取 (`tc_template/inspect.py`)

TcXaeShell 的 COM 接口不包含 DTE2, 因此 `ToolWindows.ErrorList.ErrorItems` 不可用。采用 COM + pyautogui 混合方案:

1. COM `ExecuteCommand("View.ErrorList")` 聚焦错误列表窗口
2. pyautogui `Ctrl+A` → `Ctrl+C` 复制全选内容
3. `pyperclip.paste()` 读取剪贴板
4. 解析 tab 分隔的文本 (中英文兼容)

### AI Workflow 集成

**完整工作流模板**

```
1. tc-template create packml -n MyProject -o G:/Prj
   → 14 个模板可选, 一键创建 + 打开 + 错误检查

2. tc-template plc list G:/Prj/MyProject/PLC1
   → 列出所有 FB/DUT/GVL/VISU, 确认项目结构

3. COM CreateChild → 文件系统 write_pou → Ctrl+S 刷新
   → 创建新 FB/Struct/GVL, 写入代码

4. tc-template plc read/write + pyautogui live edit
   → 读写代码, 编辑器实时反馈

5. tc-template tc deploy
   → 编译 → 激活 → 重启 → 登录 → 启动, 一键部署
```

**技术栈**

| 层级 | 技术 | 用途 |
|------|------|------|
| AI Agent | Claude Code + 3 Skills | 自然语言编程 |
| AI 精选知识库 | Markdown/PDF + catalog.yaml | 提供可追溯的领域摘要、工程契约和 Agent 工作流；精确 API 通过 docs_search/docs_read 回查 |
| Python 引擎 | 13 模块, Click CLI | 命令调度 |
| COM 接口 | win32com + TcXaeShell.DTE.15.0 | 创建 POU, 配置激活 |
| GUI 自动化 | pyautogui + pyperclip | 编辑器读写, 弹窗处理 |
| XML 处理 | ElementTree + CDATA marker | .TcPOU 文件读写 |

## 错误处理与边界情况

| 场景 | 处理 |
|------|------|
| 模板不存在 | TemplateNotFoundError + 列出可用模板 |
| 输出目录已存在 | scaffold 自动覆盖创建 |
| PLC 名称中文 | 树迭代避免路径字符串拼接 |
| CDATA 转义 | marker-based 序列化保护 `<![CDATA[...]]>` |
| COM 连接失败 | 多 ProgID 重试 (TcXaeShell.DTE.15.0 → VS DTE 17.0) |
| 弹窗阻塞 COM | 后台 pyautogui 线程持续点击 OK |
| Login 失败 | deploy 中 try/except 跳过, 不中断整个流程 |
| SPT 模板嵌套目录 | `analyze_project` 支持 `glob("*/*.tsproj")` |

## 经验教训

1. **COM 创建 POU > pyautogui 菜单导航** — COM 一步到位, pyautogui 菜单不可靠
2. **vInfo='ST' 不是 ['6']** — 数组格式错误生成 NWL
3. **树路径不用中文** — 用迭代 `for sub in nested` 避免编码问题
4. **文件系统 > COM 读写代码** — XML CDATA 处理比 COM 接口更可靠
5. **SilentMode + 后台点击 = 无人值守部署** — 解决 TwinCAT 弹窗阻塞 COM 调用的问题
