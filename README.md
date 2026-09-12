# TwinCAT Agent

AI-driven TwinCAT 3 automation platform — template management, PLC programming, and runtime control.

**AI 驱动的 TwinCAT 3 自动化平台 — 模板管理 · PLC 编程 · 运行时控制**

[![Gitee](https://gitee.com/Ethan-Tian/twin-cat-agent.svg)](https://gitee.com/Ethan-Tian/twin-cat-agent)
[![GitHub Release](https://img.shields.io/github/v/release/AutomationAgent-Code/TwinCAT-Agent?label=GitHub%20Release)](https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest)

---

## Architecture

```
TwinCAT_Agent/
├── tc_template/                    # Python package (core engine)
│   ├── cli.py                      # Click CLI (template/plc/tc/fblib/case)
│   ├── scaffold.py                 # Project scaffolding from templates
│   ├── extract.py                  # Template extraction + GUID protection
│   ├── guid_utils.py               # GUID scan / classify / replace
│   ├── plc.py                      # PLC code read/write (COM + filesystem)
│   ├── _com.py                     # Force dynamic COM dispatch (Python 3.14 fix)
│   ├── inspect.py                  # Error list reader (pure COM)
│   ├── tc_platform.py              # Build / activate / deploy / mode control
│   ├── models.py                   # TemplateMetadata + YAML serialization
│   ├── repository.py               # Template CRUD
│   ├── hooks.py                    # Lifecycle hook executor
│   └── composer.py                 # Multi-template composition (WIP)
├── tc_agent/                       # Local Agent backend (209 tools, gates, SQLite conversations)
│   ├── backend.py                  # HTTP/WS service, streaming turns, reconnect and approvals
│   ├── agent_core.py               # Provider adapters, tool registry, permission/quality gates
│   ├── mcp_client.py               # User-configured stdio / Streamable HTTP MCP bridge
│   ├── runtime.py                  # Versioned Action/Result envelopes and idempotency keys
│   └── conversation_store.py       # Messages + durable Run/Step/Tool/Approval ledger
├── tc_agent_vsix/                  # TwinCAT XAE extension and WebView host
├── .claude/commands/               # Claude Code skill definitions
│   ├── twincat-template.md         # /twincat-template
│   ├── plc-programming.md          # /plc
│   └── twincat-platform.md         # /tc
├── knowledge_base/                 # 精选知识源与来源目录
│   ├── catalog.yaml                # 来源、权威级别、主题和验证边界
│   ├── README.md                   # 知识库维护与检索规则
│   ├── tc3_automation_interface.md # COM API 摘要
│   ├── tc3_hmi_engineering.md      # TE2000/HMI 工程契约
│   ├── tf55xx_tc3_mc3.md           # MC3 结构化索引
│   └── twincat-ai-workflow.md      # Agent 工作流与可信度边界
├── reference/                      # Source projects for template extraction
├── Repository/                     # Versioned TwinCAT project templates
├── fblib/                          # Reusable PLC function-block templates
├── output/                         # Generated projects (gitignored)
└── pyproject.toml                  # Package config (click, pyyaml, jinja2)
```

PLC 源码还可以通过 Agent 的 `plc_git_sync` 工具从 GitHub 拉取并直接写入当前已打开
的 XAE，不需要关闭或重新打开 XAE。详见 [docs/plc_git_sync.md](docs/plc_git_sync.md)。

当前运行链路为：XAE VSIX/WebView2 → 本地 HTTP 8766 + WebSocket 8765 →
`tc_agent` 模型与工具循环 → `tc_template` Native COM/PowerShell 兼容桥 →
TwinCAT Automation Interface/ADS。内嵌 Agent 直接调用工具引擎；MCP Server
作为外部宿主入口独立保留。每个项目的 `.TwinCATAgent/agent.db` 同时保存消息历史
和 Run/Step/Tool/Approval 执行账本；中断恢复不会自动重放结果未知的写操作。
旧 `.tc_agent_history.json` 只用于首次迁移，不再是当前存储。

用户可在 Agent“设置 → 第三方 MCP 服务”中接入自己的 PLC 模板库或内部工具，
支持本地 stdio 与 Streamable HTTP。外部工具始终需要逐次确认；配置方法和安全边界见
[`docs/external_mcp.md`](docs/external_mcp.md)。

---

## 最新安装包

当前稳定版为 **v1.0.8.109**。Windows 客户端可直接下载：

[下载 TwinCAT Agent v1.0.8.109](https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest/download/TwinCAT-Agent-Setup-v1.0.8.109.exe)

安装包 SHA256：

`4FBEDE2B4ADA6653025E3345B883FF8FEDAD0FEFB1F8BDD42B9B668A860544F9`

也可以从 [GitHub Releases](https://github.com/AutomationAgent-Code/TwinCAT-Agent/releases/latest) 查看版本说明和校验文件。

安装程序包含 TwinCAT Agent 后端、32 位 COM 辅助运行时、TE2000/TwinCAT HMI 工具及 TwinCAT XAE 嵌入扩展。
升级时会保留 Provider 配置、项目会话数据库、授权文件和用户工程。

---

## Quick Start

### Update an existing installation without the installer

After the first installation, update this computer directly from the checkout:

```powershell
& .\scripts\Update-LocalInstallation.ps1
```

The updater stops the backend, creates a rollback backup, refreshes the agent,
TwinCAT tools, and built-in cases, then restarts the backend. API settings,
history, licenses, and ProgramData cases are preserved. Use `-DryRun` to check
only, or `-IncludeExtension` to update the XAE extension (restart XAE afterward).
An extracted portable package can be used with `-PackageDir <folder>`.

Provider/model settings are stored independently from program files at
`%LOCALAPPDATA%\TwinCAT Agent\config.json`. Source and installed backends use
the same file, so changing execution mode during a local update does not switch
to an older provider list. Legacy module-local configuration is migrated once
when the stable file is absent.

```bash
# Install
cd TwinCAT_Agent
pip install -e .

# Explore templates
tc-template list

# Create a project (scaffold → open → inspect)
tc-template create packml -n MyProject -o G:/Prj

# Edit the currently open PLC project through TwinCAT COM
tc-template plc read MAIN
tc-template plc write MAIN "nCounter := nCounter + 1;" --area implementation

# Build & deploy
tc-template tc deploy
```

Run the offline test suite with:

```bash
pip install -e ".[agent,test]"
python -m pytest
```

---

## Skills

### `/twincat-template` — Template Management

| Command | Description |
|---------|-------------|
| `list` | List all templates with auto-generated descriptions |
| `info <name>` | Template metadata, variables, GUID strategy |
| `create <tpl> -n <name> -o <dir>` | Scaffold → COM open → error check |
| `add <path> -n <name>` | Extract template from TwinCAT project |
| `inspect` | Read error list from open TwinCAT |
| `validate <name>` | Validate template integrity |
| `remove <name>` | Delete a template |

**Key features:**
- Whitelist-based GUID protection (Beckhoff system GUIDs preserved)
- `_Libraries` copied as-is, zero processing
- Auto-generated descriptions from MAIN POU code analysis
- Project-specific GUID → `{{GUID_N}}` placeholderization

### `/plc` — PLC Programming

| Command | Description |
|---------|-------------|
| `list` | List objects in the currently open PLC project through COM |
| `read <name>` | Read declaration + implementation + methods through COM |
| `write <name> "code"` | Write code through COM (IDE automatically reparses) |
| `export <dir> <name> -o <xml>` | Export PLCopen XML |
| `import <dir> <xml>` | Import PLCopen XML |
| `create-com <name> -t fb` | Create POU/DUT/GVL via COM |
| `reload` | Compatibility no-op; COM writes already trigger re-parse |
| `build` | Build + error list |
| `import-com` / `export-com` | PlcOpenImport/Export via COM |
| `libraries` | List library references |

**Supported object types:** POU (Program/FB/Function), DUT (Struct/Enum/Union), GVL, VISU, Interface.

**COM CreateChild reference:** `Program=602`, `Function=603`, `FB=604`, `Enum=605`, `Struct=606`, `GVL=615`, `Visu=619`.

### `/tc` — Platform Control

| Command | Description |
|---------|-------------|
| `build` | Build PLC project + show errors |
| `check` | Check all PLC objects (compile check) |
| `activate` | Activate config + restart TwinCAT (does not switch to Config mode) |
| `login` / `logout` | Login/logout PLC runtime |
| `start` / `stop` | Start/stop PLC program |
| `online` | Login + Start in one step |
| `state` | Show runtime state (Config/Run/Stop) |
| `config` | Switch to Config mode |
| `run` | Switch to Run mode |
| `target` | Show current target NetId |
| `boot` | Set PLC as boot project |
| `realtime-info` | Read version-aware Router/ADS/Stack/Core memory, cores and tasks |
| `realtime-validate` | Read-only consistency check for RT memory, cores, priorities and task timing |
| `realtime-settings-set <json>` | Preview/write global and per-core real-time settings |
| `task-settings-set <path> <json>` | Preview/write task priority, cycle and watchdog settings |
| `hmi info` / `hmi structure` | Read the open TwinCAT HMI project metadata and file inventory |
| `hmi read <file>` / `hmi ads-info` | Read HMI markup/controls/bindings and ADS runtime configuration; use `--control-id <id> --no-content` for focused reads |
| `hmi validate` | Validate startup view, markup IDs/types and ADS JSON without running a client |
| `hmi create-view <name>` | Preview/create a `.view` or `.content` page and reload it in XAE |
| `hmi project-api <operation>` | Inspect or call the installed TE2000 `ITcHmiProject` official API; mutating operations require `--apply` |
| `hmi control <file> <action> <id>` | Preview/add/update/remove a control with XAE reload and readback |
| `hmi delete-view <file>` | Preview/back up/delete a non-startup page and remove its project registration |
| `hmi ads-runtime-set <name>` | Preview/upsert/remove an ADS Runtime while preserving saved symbol mappings |
| `hmi ads-symbols` / `hmi ads-symbol-set` | Read or safely edit schema-backed ADS symbol mappings |
| `hmi bind-plc` | Resolve the actual XAE target/PLC port, parse TMC and atomically generate Runtime mappings and schemas |
| `hmi ads-live-check` | Read-only verification of configured dynamic symbols against the actual PLC ADS endpoint and type |
| `hmi binding-diagnose` | Read-only end-to-end diagnosis of expressions, TMC export, Server mapping/schema, endpoint and ADS reachability |
| `hmi bindings` | Audit SymbolExpressions against Runtime mappings, internal symbols and control IDs |
| `hmi internal-symbols` / `hmi internal-symbol-set` | Read or safely edit Framework-schema internal symbols |
| `hmi localizations` / `hmi localization-set` | Audit or safely edit localization keys across registered locales |
| `hmi themes` / `hmi themed-resource-set` | Read themes or safely edit project-level themed resources |
| `hmi active-theme-set <theme>` | Preview/set the startup active theme with XAE reload/readback |
| `hmi user-controls` / `hmi user-control-create` | Inspect or preview/create a schema-backed UserControl |
| `hmi user-control-parameter-set` | Preview/upsert/remove a `data-tchmi-*` UserControl parameter |
| `hmi user-control-delete` | Preview/back up/delete UserControl markup, parameter file and registrations |
| `hmi framework-templates` / `hmi framework-validate` | Inspect installed TE2000 templates or validate a Framework Control project |
| `hmi framework-control-info <source>` | Read one control's attributes, functions, events and source contract |
| `hmi framework-attribute-set` / `hmi framework-event-set` | Synchronize Description.json with generated accessor/event code |
| `hmi framework-create <name> --output <dir>` | Preview/create a native1.12 TypeScript/JavaScript Framework Control scaffold |
| `hmi framework-pack <source>` | Preview/create and inspect a local `.nupkg` without installing or publishing it |
| `hmi framework-packages` / `hmi framework-package-inspect <nupkg>` | Inventory project packages or validate one local Framework package |
| `hmi framework-install <nupkg>` | Preview/transactionally install a package; apply requires explicit package-change acknowledgement |
| `hmi framework-uninstall <id>` | Preview/transactionally uninstall a non-core package with markup reference gate and rollback |
| `hmi runtime-info` | Discover the active Engineering Server, endpoints, virtual directories and real app URL |
| `hmi server-control <start\|stop\|restart>` | Preview/control the exact project's Engineering Server through XAE project reload and health readback |
| `hmi browser-validate` | Load the running HMI in hidden Edge and report runtime, network and viewport diagnostics |
| `hmi build` | Build the HMI project through DTE without focusing/copying the Error List |
| `safety-structure` / `safety-info` | Read the TwinSAFE TISC tree and project metadata |
| `safety-files` / `safety-target-info` | Read the Safety file model and target configuration |
| `safety-aliases` / `safety-application` | Read Alias Devices, SAL/Safety C structure, FB ports and wiring |
| `safety-logic-check` | Check stored SDS IDs and graphical FB/port/wire references |
| `safety-validate [source]` | Structurally check TISC or a `.splcproj`/`.tfzip` template |
| `safety-import <source>` | Preview/import a Safety template; hard confirmation required |
| `safety-create <name>` | Materialize an installed Beckhoff Safety template and import it |
| `safety-export` / `safety-remove` | Export, or explicitly remove from TISC while preserving source files |
| `safety-delete` | Default deletion path: back up and delete TISC plus files, including orphan directories |
| `silent` | Enable/disable silent mode |
| **`deploy`** | **Full pipeline: build → boot → activate → restart → login → start** |

**Deploy features:**
- Silent mode suppresses most dialogs
- Deploy does not switch to Config mode; use Config only for an explicit mode change or hardware scan preparation
- Background thread dismisses confirmation popups via win32 messages (no mouse)
- Full error list readback after each step

---

## Core Technology

| Layer | Tool | Role |
|-------|------|------|
| **COM** | `win32com` + `TcXaeShell.DTE.15.0` | Create POU, **read/write code**, activate config, start/stop runtime, navigate tree |
| **Filesystem** | XML CDATA parse/write | Offline code read/write with CDATA preservation |
| **pyautogui** | screenshot / `WM_SETTEXT` | `tc target add` route-registration GUI only (not code editing) |

### GUID Protection Strategy

When extracting templates, only project-specific GUIDs are placeholderized:
- `Id="..."`, `ProjectGUID="..."`, `ProjectGuid="..."`
- `<Application>`, `<TypeSystem>`, `<LibraryReferences>` elements
- `GuidA`/`GuidB`/`TmcHash` in `.tsproj`

Protected (never replaced):
- `{00000000-...}` null GUID (TwinCAT sentinel)
- `{18071995-*}` Beckhoff Type System
- `{B1E792BE-...}` TcXaeShell, `{08500001-...}` TcPlc30 CLSID
- `<ProjectExtensions>`, `<PlaceholderReference>`, `<Licenses>`, `<Device>` sections

---

## Dependencies

```toml
requires-python = ">=3.9"
dependencies = [
    "click >= 8.0",
    "pyyaml >= 6.0",
    "jinja2 >= 3.0",
]
# Optional (for COM / pyautogui features)
# pywin32, pyautogui, pyperclip
```

---

## License

MIT
