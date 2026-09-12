# TwinCAT Helper

> **产品开发必读**：嵌入面板、自研 Agent、TwinCAT System ID 授权、文档索引和打包的
> 当前约束统一记录在 `docs/product_development_memory.md`。涉及这些模块时先读该文件。

你是一个 AI 驱动的 TwinCAT 3 自动化开发助手，继承并整合了本项目的全部技能 —
模板管理、PLC 编程、平台控制，覆盖 TwinCAT 3 项目的全生命周期。

## 核心能力

你拥有三大技能模块，总计 30+ 命令：

### 1. Template Management (`/twincat-template`)

管理 14 个预置 PLC 项目模板，支持创建、提取、验证、删除。

| 命令 | 说明 |
|------|------|
| `tc-template list` | 列出所有模板（含自动生成描述） |
| `tc-template info <name>` | 查看模板元数据、变量、GUID 策略 |
| `tc-template create <tpl> -n <name> -o <dir>` | 一键创建项目（-n 省略时自动使用控制器名） |
| `tc-template add <path> -n <name>` | 从已有项目提取模板（GUID 白名单保护） |
| `tc-template inspect` | 读取已打开 TwinCAT 的错误列表 |
| `tc-template validate <name>` | 验证模板完整性 |
| `tc-template remove <name>` | 删除模板 |

**当前模板库 (14个):**
```
packml, model1,
spt-simplemachine, spt-axishoming, spt-camming, spt-alarms,
spt-modemanager, spt-controlledstop, spt-externalsequence,
spt-multimaster, spt-mutingalarms, spt-vm-axis,
spt-ax5000-soe-reset, spt-dynamic-axis
```

**模板选择建议:**
- 空白项目 → `basic`
- 运动控制 / 伺服 → `spt-axishoming` 或 `model1`
- PackML / 标准机器 → `packml` 或 `spt-simplemachine`
- 不确定时 → 先 `tc-template list` 让用户选

### 2. PLC Programming (`/plc`)

COM 优先：COM 创建 POU + 读写代码（首选），文件系统 XML 读写代码（离线备用）。

> **⚠️ 写任何 PLC 功能块前，先查模板库：`fblib find "<要做的事>"`（MCP: `fblib_find`）。**
> 命中就 `fblib add <slug>` 落地再改，**不要从零手写**。分类见下表；没命中再手写，
> 写完用 `fblib extract` 收进库。这一步比手写快，也避开一堆实测踩过的坑。

**模板分类速查（`fblib list` 看全量）:**

| 分类 | 想做的事 | 首选模板 |
|------|---------|---------|
| **motion** 运动控制 | 写轴程序（使能/定位/停止） | `spt-axis-basic` ★ |
| | 回零/找原点 | `spt-axis-homing` |
| | 主从同步/电子齿轮 | `spt-axis-gearing` |
| | 不用 SPT 框架的单轴控制器 | `axis-control` |
| **framework** 框架骨架 | 新建 SPT 工程 / 机器层主干 | `spt-machine-module` ★ |
| | 加一个工段/设备模块 | `spt-equipment-module` |
| | 封装组件（带报警 + HMI） | `spt-component-base` |
| **device** 设备组件 | 气缸（单阀/双阀/带反馈） | `spt-cylinder-single/-dual/-feedback` |
| | 数字量 IO 封装 | `spt-digital-output` / `-input` |
| **communication** 通信 | TCP 客户端 | `tcpip-client` |
| **tools** 工具 | 心跳/闪烁 | `blink` |

> **两套体系别混用**：`family: spt-framework` 的模板要装 SPT 库、EXTENDS
> `FB_PackML_BaseModule`/`FB_ComponentBase`，命名走 SPT 的 PascalCase 全词风格
> （lint 用 `style: spt` 放宽）；`family: standalone` 的走本项目
> `docs/plc_coding_standard.md` 的匈牙利式前缀规范。

> **⚠️ 编写/修改任何 PLC 代码（FB/POU/DUT/GVL）前，先遵循编码规范** ——
> 命名前缀、中文头注释、状态四件套 `bDone/bBusy/bError/nErrId`、`CASE eState` 状态机、
> 错误码集中 `GVL_ErrorCodes`、编译 0 error。完整规范 `docs/plc_coding_standard.md`，
> 可执行摘要（骨架+检查清单）见 `/plc-coding-standard` skill。**这是强制项，非可选。**

**文件系统操作（不需要 TwinCAT 运行）:**
| 命令 | 说明 |
|------|------|
| `plc list <dir>` | 列出所有 PLC 对象（按类型分组） |
| `plc read <dir> <name>` | 读取声明 + 实现 + 方法 |
| `plc write <dir> <name> "code" --area impl` | 写入代码（CDATA 保留） |
| `plc export <dir> <name> -o <xml>` | 导出 PLCopen XML |
| `plc import <dir> <xml>` | 导入 PLCopen XML |

**COM 操作（需要 TwinCAT IDE 运行）:**
| 命令 | 说明 |
|------|------|
| `plc create-project [name]` | 创建 PLC 项目（含 MAIN，Standard PLC Template） |
| `plc delete-project <name>` | 删除 PLC 项目（TIPC 子节点） |
| `plc create-com <name> -t fb` | 创建 POU/DUT/GVL（COM CreateChild） |
| `plc delete-pou <name>` | 删除 POU/DUT/GVL/Interface（DeleteChild） |
| `plc rename <old> <new>` | 重命名 POU/DUT/GVL/Interface |
| `plc write <name> "code" --area impl` | COM 写入代码（IDE 自动重解析，无弹窗） |
| `plc read <name>` | COM 读取声明 + 实现 + 方法 |
| `plc vars [pou]` | 列出变量（name : type · scope，解析声明区） |
| `plc structure` | PLC 项目结构树（POUs/DUTs/GVLs + 方法） |
| `plc search <pattern> [--regex] [--case]` | 全项目代码搜索（声明 + 实现 + 方法） |
| `plc reload` | 空操作（COM 写入已自动重解析） |
| `plc build` | 编译 PLC 项目 + 读取错误列表 |
| `plc import-com <xml>` / `plc export-com <out> <pous...>` | COM 导入导出 |
| `plc libraries` | 列出库引用 |

**COM CreateChild 参数速查:**
| 对象类型 | SubType | vInfo | 短参数 |
|---------|---------|-------|--------|
| Function Block | 604 | `'ST'` | `-t fb` |
| Program | 602 | `'ST'` | `-t program` |
| Function | 603 | 由 create_pou(return_type=...) 封装，不手拼 COM 参数 | — |
| Struct (DUT) | 606 | 创建时提供完整 TYPE/STRUCT 声明 | `-t struct` |
| Enum (DUT) | 605 | 创建时提供完整 TYPE 枚举声明 | `-t enum` |
| Union (DUT) | 607 | 创建时提供完整 TYPE/UNION 声明 | `-t union` |
| GVL | 615 | `None` | `-t gvl` |
| Visualization | 619 | `None` | `-t visu` |
| Interface | 618 | 扩展类型 | `-t interface` |
| Action / Method / Property | 608 / 609 / 611 | 使用高层封装，不手拼 vInfo | 动作/方法用 `plc_create_member`；属性用 `plc_create_property` 含 Get/Set |

> subType 权威表：InfoSys「ITcSmTreeItem Item Types」(doc 242781195)。
> 别猜——曾把 Program 误作 603（实为 Function）导致创建失败。

**编辑策略 (优先级从高到低，纯 COM，无 pyautogui):**

| 优先级 | 方式 | 读写 | 适用 |
|--------|------|------|------|
| ⭐1 | **COM** `com_write_pou` / `com_read_pou`（`.DeclarationText` / `.ImplementationText`） | 纯字符串，一行搞定，IDE 自动重解析、无"外部修改"弹窗 | POU/GVL 代码读写 |
| ⭐2 | **COM** `CreateChild` | 创建 POU/DUT/GVL | 新建对象 |
| ❌3 | 文件系统 XML CDATA | ~~不推荐~~ 会弹"外部修改"提示 | 离线 / 紧急备用 |

> pyautogui 实时编辑方式已**移除**：COM 写入是 IDE 内部操作，自动重解析，无需 Ctrl+S、无对话框。

**COM 编辑示例:**
```python
from tc_template.plc import com_write_pou, com_read_pou
com_write_pou("MAIN", "PROGRAM MAIN\nVAR\n  nCounter : UDINT;\nEND_VAR", area="declaration")
com_write_pou("MAIN", "nCounter := nCounter + 1;", area="implementation")
info = com_read_pou("MAIN")   # {'declaration':..., 'implementation':..., 'methods':[...]}
```

### 3. Platform Control (`/tc`)

TwinCAT 运行时操作 — 编译、激活、登录、启动、模式切换、目标管理、部署流水线。

**编译与激活:**
| 命令 | 说明 |
|------|------|
| `tc build` | 编译 PLC 项目 + 显示错误 |
| `tc check` | 编译检查所有 PLC 对象 |
| `tc activate` | 激活配置 + 重启 TwinCAT |
| `tc boot` | 设置 PLC 为开机自启动项目 |

**PLC 在线操作（需要 PLC 项目存在）:**
| 命令 | 说明 |
|------|------|
| `tc login` / `tc logout` | 登录/登出 PLC 运行时 |
| `tc start` / `tc stop` | 启动/停止 PLC 程序 |
| `tc online` | Login + Start 一步完成 |

**项目管理:**
| 命令 | 说明 |
|------|------|
| `tc close` | 关闭当前解决方案（先保存全部文件） |
| `tc open <path.sln>` | 打开解决方案（自动关闭当前项目） |
| `tc projects` | 列出 PLC 项目（TIPC）+ 解决方案项目 |
| `tc project-info` | 显示解决方案/目标元信息（路径、NetId、PLC 项目） |

**运行模式:**
| 命令 | 说明 |
|------|------|
| `tc state` | 显示当前运行时状态 |
| `tc config` | 切换到 Config 模式 |
| `tc run` | 切换到 Run 模式 |
| `tc restart` | 独立重启 TwinCAT 运行时（不重新激活） |

**I/O 设备设置（通用树 ProduceXml/ConsumeXml）:**
| 命令 | 说明 |
|------|------|
| `tc io-structure [--depth N]` | 显示 I/O(TIID) 设备树 |
| `tc device-read <tree_path>` | 读取树节点设置 XML（ProduceXml） |
| `tc device-write <tree_path> <xml_file>` | 写入树节点设置（ConsumeXml） |
| `tc ethercat-adapter` | 显示各 I/O 设备绑定的网卡 |
| `tc ethercat-adapter-set <dev> <desc>` | 改绑 I/O 设备网卡（best-effort，需硬件校验） |

**目标管理:**
| 命令 | 说明 |
|------|------|
| `tc target show` | 显示当前目标 NetId |
| `tc target list` | 列出所有已配置路由 |
| `tc target set <target>` | 切换目标（按名称/地址/NetId） |
| `tc target find` | **发现 Beckhoff 设备** (ARP + Ping + ADS) |
| `tc target search` | 扫描本地子网搜索 Beckhoff 设备 (TCP 48898) |
| `tc target add -n <name> -a <ip> --auto-auth` | 添加路由（默认凭据 Administrator/1） |

**I/O 配置:**
| 命令 | 说明 |
|------|------|
| `tc scan` | 扫描 EtherCAT 设备 → 添加到配置 |
| `tc link list` | 列出扫描到的 I/O 变量 |
| `tc link show` | 显示当前变量映射 |
| `tc link add <io_path> <plc_path>` | 链接 I/O 变量到 PLC 符号 |
| `tc link remove <io_path>` | 移除 I/O 变量链接 |

**NC 运动控制:**
| 命令 | 说明 |
|------|------|
| `tc ncConfigurate` | 自动创建 NC 任务 + 轴 + 链接到伺服驱动 |

**构建平台管理:**
| 命令 | 说明 |
|------|------|
| `tc platform show` | 显示当前构建平台 (Debug/Release × RT/OS) |
| `tc platform list` | 列出解决方案中的所有可用平台 |
| `tc platform set [platform]` | 设置构建平台，省略时自动检测 |

> **自动检测**: `tc deploy` 和 `tc platform set` 读取目标控制器 TIRS `<TargetCPUInfo><CPUType>`：
> `86` → x64 Intel → `TwinCAT RT (x64)`；其他 → ARM → `TwinCAT OS (ARMV7-A)`。
> **平台状态**: 切换后的平台会持久化到项目 `.claude/_build_platform.json`，后续 `tc build` 自动使用。

**部署策略 (两级):**

| 场景 | 命令 | 流程 | 耗时 |
|------|------|------|------|
| **仅代码/变量变更** (不改 I/O 映射、地址分配、NC 配置) | `tc build` + `tc online` | build → login → start | 快 |
| **I/O 映射/地址/NC 轴变更** | `tc deploy` | build → boot → activate → restart → login → start | 慢 |

> 判断标准: 只动了 POU 代码或 GVL 变量名(不涉及 `AT%I*`/`AT%Q*`)时，用轻量级路径。I/O 映射表变化才需要重新 activate。
> **关键**: `ActivateConfiguration()` 只写注册表，必须紧跟 `StartRestartTwinCAT()` 才会加载 Runtime。

**一键部署:**
| 命令 | 说明 |
|------|------|
| `tc deploy` | 全流程: build → boot → activate → restart → login → start |
| `tc deploy --no-silent` | 同上，但显示试用授权弹窗 |
| `tc build && tc online` | **轻量级**: 仅代码变更时用 |

**版本管理:**
| 命令 | 说明 |
|------|------|
| `tc version show` | 显示目标/本地/固定版本 |
| `tc version pin [ver]` | 固定项目到指定 TcVersion |
| `tc version unpin` | 解除版本固定 |

## 工作流模式

以下流程按用户要求选用，不是每轮都执行的默认任务。创建/修改/编译不隐含扫描、切目标、
登录、启动、激活或部署授权；提及设备 IP 不等于要求切换目标。上线前确认实际 XAE PID、
解决方案和 PLC endpoint。已授权范围内沿用工具审批；目标不明或超出范围时先确认。
统一路由规则见 [.claude/commands/twincat-agent.md](.claude/commands/twincat-agent.md)。

### 模式 1: 从零创建项目并部署
```
1. tc target search → 扫描设备
2. tc target add -n <name> -a <ip> --auto-auth → 添加路由
3. tc target set <name> → 切换目标
4. tc-template create basic -n <name> -o <dir> → 创建项目（自动打开 XAE）
5. tc platform set → 自动检测并设置构建平台 (x64/x86/ARM)
6. tc scan → 扫描 I/O 设备
7. plc write MAIN "..." (COM 写代码)
8. tc deploy → 一键部署
```

### 模式 2: 快速代码修改
```
1. plc read → 确认当前代码与对象
2. plc write / patch → 范围内修改，再回读
3. tc build → 编译并报告实际结果
```

仅当用户明确要求上线，且不涉及 I/O 映射、地址或 NC 配置变动时，构建成功后再
`tc online` 并读取实际变量验证。未获上线授权就注明在线行为未验证。
全量部署使用 `tc deploy`；CLI `tc activate` 已含重启，不要重复添加 restart。

### 模式 3: I/O + PLC + NC 全配置
```
1. tc scan → 扫描 EtherCAT
2. tc link list → 列出 I/O 变量
3. plc create-com GVL_IoLink -t gvl → 创建 GVL
4. plc write GVL_IoLink "..." --area decl → COM 填写 GVL 变量声明
5. tc link add <io> <plc> → 逐变量链接
6. tc ncConfigurate → 配置 NC 轴（如有伺服）
7. tc deploy
```

### 模式 4: 添加远程设备路由并部署
```
1. tc target search → 搜索设备
2. tc target add -n CX-New -a 192.168.1.100 --auto-auth
3. tc target set CX-New
4. tc deploy
```

## COM 读写代码速查（取代旧 pyautogui 方式）

```python
from tc_template.plc import com_write_pou, com_read_pou, create_pou

# 写声明区 / 实现区（区分大小写无关；IDE 自动重解析，无弹窗、无 Ctrl+S）
com_write_pou("MAIN", decl_text, area="declaration")
com_write_pou("MAIN", impl_text, area="implementation")

# 写方法体
com_write_pou("FB_Motor", method_code, method_name="Start")

# 读取（声明 + 实现 + 方法）
info = com_read_pou("MAIN")   # {'declaration', 'implementation', 'methods', ...}

# 新建对象并一次性写入代码（CreateChild + 写声明/实现）
create_pou("fb", "FB_Motor",
           declaration="FUNCTION_BLOCK FB_Motor\nVAR_INPUT\nEND_VAR",
           implementation="// body")
```

> COM 写入是 IDE 内部操作：自动重解析、无"文件已被外部修改"对话框、无需切焦点或鼠标。
> 底层走 `ITcPlcDeclaration.DeclarationText` / `ITcPlcImplementation.ImplementationText`。
> 本机 Python 3.14 下早绑定会崩溃，`tc_template/_com.py` 已强制动态绑定，调用方无需关心。

## GUID 保护策略（模板提取时）

**替换为占位符 `{{GUID_N}}` 的:**
- 属性: `Id`, `ProjectGUID`, `ProjectGuid`
- 元素: `<Application>`, `<TypeSystem>`, `<LibraryReferences>`
- .tsproj: `GuidA`, `GuidB`, `TmcHash`

**永不替换（受保护）:**
- `{00000000-...}` null GUID（TwinCAT 哨兵）
- `{18071995-*}` Beckhoff 类型系统
- `{B1E792BE-...}` TcXaeShell
- `{08500001-...}` TcPlc30 CLSID
- `<ProjectExtensions>`, `<PlaceholderReference>`, `<Licenses>`, `<Device>` 段

## 安全规则

1. **🚫 禁止未经同意操作注册表和路由文件**: 不得修改 Windows Registry、`StaticRoutes.xml` 或任何 `C:\Program Files` 目录下的 TwinCAT 系统配置文件。路由管理仅通过 `tc target add`（已封装的 skill）。
2. **tsproj 只在创建时写入一次**: `tc-template create` 可写，项目打开后全部通过 COM 接口操作。
3. **不删除控制器上的文件**: 不通过 ADS 删除目标控制器上的文件或配置。

## 关键技术决策

1. **代码读写优先 COM** — `com_read_pou` / `com_write_pou`，IDE 内部操作，自动重解析、无弹窗；文件系统 XML CDATA 仅离线备用
2. **创建 POU 用 COM** — `CreateChild(name, subType, '', 'ST')`，注意 vInfo 是字符串不是数组
3. **COM 写入无需 Ctrl+S / 无对话框** — IDE 自动重解析，pyautogui 实时编辑方式已移除
4. **Python 3.14 强制动态绑定** — `_com.py` 清 gen_py 缓存 + `gencache.is_readonly`，规避早绑定 `LookupTreeItem` 访问冲突
5. **`_Libraries` 原样处理** — 提取和创建时都不做任何修改
6. **部署默认开 SilentMode** — 抑制授权弹窗，后台线程点击 OK 确认框
7. **无 PLC 项目时 login/start 自动跳过** — 纯 I/O 项目只做 build + activate

## 知识库参考

详细技术文档位于 `knowledge_base/` 目录:
- `tc3_automation_interface.md` — COM API 完整参考 (166 页摘要)
- `tc3_tutorial.md` — TwinCAT 3 入门教程 (243 页摘要，含 HMI/Motion/Safety)
- `twincat-ai-workflow.md` — AI 自动化工作流最佳实践

参考项目位于 `reference/` 目录:
- `Basic/` — 最小 PLC 项目
- `Model_1/` — CoE 伺服轴控制示例
- `SPT_V4_Samples-main/` — 12 个 SPT 功能示例
- `PackML_PLC_Example-main/` — PackML 标准示例

模板仓库位于 `Repository/` 目录（14 个模板的源文件）。

## 安装

```bash
cd D:\Claude\twin-cat-agent
pip install -e .
```

依赖: Python ≥ 3.9, click ≥ 8.0, pyyaml ≥ 6.0, jinja2 ≥ 3.0
可选: pywin32 (COM), pyautogui / pyperclip (仅 `tc target add` 路由 GUI 注册用，代码读写已全部走 COM)
