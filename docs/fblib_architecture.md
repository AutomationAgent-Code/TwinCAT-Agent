# FB 库（fblib）架构

> 版本：1.0 · 2026-07-26
> 目的：把常用功能块（TCP/IP、Modbus、回零、Blink…）封装成**可参数化的文本模板**，
> 一键塞进打开的 PLC 项目；并能从项目**反向抽取**成模板。方便后续封装和修改。

## 1. 定位：三层模板体系

| 层 | 粒度 | 载体 | 例 |
|---|---|---|---|
| 项目模板 | 整解决方案 | `Repository/` + `scaffold.py` | packml、spt-axishoming |
| **FB 库（本文）** | **单个功能块 + 配套类型/库依赖** | **`fblib/` + `fblib.py`** | FB_TcpIpClient |
| FB 骨架 | 空白标准骨架 | `fb_scaffold.py` | 空事务型 FB |

fblib 填补中间层：一批**具体、测过、合规、可参数化**的功能块。

## 2. 存储：文本 decl/impl 为主

选文本而非 PLCopen XML 的理由：
- 塞入走 `com_new_pou(decl, impl)` —— IDE 内部操作、自动重解析、无弹窗、无 GUID/XML 折腾。
- 参数化 = 简单 `{{占位符}}` 文本替换。
- 可读、可 diff、**手改方便**。

OOP 复杂 FB（带方法/属性/接口，文本难保真）走**逃生通道**：`manifest.format: plcopen`，
存整份 PLCopen `.xml`，塞入走 `PlcOpenImport`。默认 `format: text`。

## 3. 目录结构（每 FB 一文件夹）

```
fblib/
  <slug>/                    # 如 tcpip-client
    manifest.yaml            # 元数据 + 参数 + 依赖 + 配套类型
    <FB_Name>.decl           # 声明区（含 {{占位符}}）
    <FB_Name>.impl           # 实现区
    types/                   # 配套 DUT（枚举/结构），每个一 .decl
      <E_Name>.decl
    methods/                 # 方法（v1.1；单体 FB 可无此目录）
      <Method>.decl / <Method>.impl
    <FB_Name>.xml            # format:plcopen 时用（替代 decl/impl）
    README.md                # 用法 + 调用示例
```

### manifest.yaml schema

```yaml
name: FB_TcpIpClient           # FB 名（FB_ 前缀）
slug: tcpip-client             # 文件夹名（kebab-case）
category: communication        # 见 fblib/catalog.yaml：motion|framework|device|communication|tools
family: standalone             # standalone | spt-framework（两套体系，别混用）
style: default                 # default | spt（选 lint 规则集）
rank: 10                       # 同分类内排序，小的先出；旗舰模板给 1
archetype: transaction         # transaction | cyclic | oop（对应编码规范原型）
format: text                   # text（默认）| plcopen
description: TCP/IP 客户端，封装 socket 连接/收发/断开
use_when: PLC 要主动连一个 TCP 服务器收发数据    # 一句话选型依据
keywords: [tcp, socket, 客户端, 通信]            # fblib find 的匹配词（中英混写）
version: "1.0"
libraries: [Tc2_TcpIp]         # 依赖的 TwinCAT 库 → add 时自动 com_add_library
companions: [E_TcpIpState]     # 配套 DUT，一并创建（对应 types/*.decl）
requires: [spt-digital-output] # 依赖的其他模板 slug，add 时递归塞入
notes: |                       # 落地后还要人工做的事（如 tmc 事件类、NC 链接）
  ...
params:                        # 可参数化占位符
  - {name: RX_BUFFER_SIZE, default: "1024", desc: 接收缓冲字节}
  - {name: TIMEOUT,        default: "T#5S", desc: 连接超时}
```

**objects 模式**（多对象 OOP 模板）里对象名可参数化，磁盘文件名另给：

```yaml
objects:
  - name: "{{MODULE_NAME}}"    # 渲染后的实际对象名
    file: AxisModule           # 磁盘上的 .decl/.impl 与成员目录名（不含占位符）
    kind: fb
    members:
      - {name: CyclicLogic, kind: method, return_type: ""}
      - {name: AxisRef, kind: property, return_type: "REFERENCE TO AXIS_REF", accessors: [Get]}
```

### 分类与选型（fblib/catalog.yaml）

`catalog.yaml` 定义分类顺序和**意图路由**：用户话里出现 `routes[].when` 里的词，
该分类整体加权。`fblib find "<意图>"` 据此排序 —— 打分 = 分类命中 10 +
关键词命中 4（每多一个 +2，上限 12）+ 名称 4 + 描述 2 − rank/100。
关键词命中按数量加权是刻意的：查"回零"时 `spt-axis-homing` 必须压过同分类
旗舰的 `spt-axis-basic`。

### 两套风格

| family | 基类 | 命名 | lint |
|---|---|---|---|
| `spt-framework` | `FB_PackML_BaseModule` / `FB_ComponentBase`，需装 SPT 库 | SPT 式：只有指针/接口带前缀（`p`/`ip`），其余 PascalCase 全词 | `style: spt` 跳过 naming-object / naming-var / fb-header |
| `standalone` | 无 | `docs/plc_coding_standard.md` 的匈牙利式 | 默认全套 |

## 4. 操作与数据流

| 操作 | CLI | MCP | 说明 |
|---|---|---|---|
| 列表 | `fblib list [cat]` | `fblib_list` | 扫 fblib/ 读 manifest |
| 详情 | `fblib info <slug>` | `fblib_info` | 元数据/参数/依赖 |
| 塞入 | `fblib add <slug> [--param K=V]` | `fblib_add` | 建到打开的项目 |
| 抽取 | `fblib extract <FB> <slug>` | `fblib_extract` | 从项目反向成模板 |
| 校验 | `fblib validate <slug>` | `fblib_validate` | lint 模板文本 |

### 4.1 `add` 数据流（塞入打开的项目）

```
load manifest + decl/impl
  → 参数替换 {{PARAM}} → 值（未给用 default）
  → 建配套类型   : com_new_pou(enum/struct, types/*.decl)
  → 建 FB        : com_new_pou(fb, decl, impl)
  → [v1.1] 建方法: com_write_pou(method_name=..., impl)
  → 加库依赖     : com_add_library(lib) for lib in libraries
  → 合规门禁     : plc_lint → 返回 findings（应为空）
```

### 4.2 `extract` 数据流（从项目反向）

```
com_read_pou(FB) → {declaration, implementation, methods}
  → 判定 archetype（有 bExecute=transaction / EXTENDS|IMPLEMENTS=oop / 否则 cyclic）
  → 推断 libraries（从项目 com_list_libraries 交集，或留空待填）
  → 写 fblib/<slug>/：<FB>.decl / <FB>.impl / manifest.yaml（含 companions 待人工确认）
  → 提示：把字面量手动改成 {{param}}（不自动参数化，避免误判）
```

## 5. 复用现有资产（几乎零重复造轮子）

| fblib 步骤 | 复用 |
|---|---|
| 建 FB / 枚举 / 结构 | `com_new_pou`（已测：建 FB+枚举编译 0-error） |
| 加库依赖 | `com_add_library`（已测：Tc2_TcpIp 往返） |
| 合规门禁 | `plc_lint`（已测：抓真问题+回归全过） |
| 反向读码 | `com_read_pou`（已测） |
| GUID 保护（plcopen 抽取） | `extract.py` 白名单 |
| 原型判定 | 编码规范 §2.1b + `lint._fb_kind` |

## 6. 合规保证

- 每个 fblib 模板**收进库前必须过 `plc lint`**（命名/头注释/状态四件套/原型）。
- 库天然合规 → `fblib add` 塞入的代码开箱即符合 `docs/plc_coding_standard.md`。

## 7. v1 边界

- **单体 FB**（事务型/循环型）优先；带方法的 OOP FB 归 v1.1 或走 plcopen 逃生通道。
- **不自动参数化**：extract 存字面量，占位符人工标注。
- 方法节点"新建"未验证 → v1 不建方法。

## 8. 现有模板（14 个，`fblib list` 看全量）

| 分类 | slug | family | 说明 |
|---|---|---|---|
| motion | `spt-axis-basic` ★ | SPT | 单轴设备模块：使能/定位/停止/急停挂 PackML 状态机 |
| motion | `spt-axis-homing` | SPT | 四种库内置回零 + 自定义 `I_MotionSequence` 例程 |
| motion | `spt-axis-gearing` | SPT | 虚拟主轴 + 从轴 `GearIn`，AXIS_REF 属性注入 |
| motion | `axis-control` | 独立 | 命令模式单轴控制器（纯 Tc2_MC2，不依赖框架） |
| framework | `spt-machine-module` ★ | SPT | Machine Module + MAIN，最小可跑骨架 |
| framework | `spt-equipment-module` | SPT | EM 骨架：Acting/Wait 状态方法分工 |
| framework | `spt-component-base` | SPT | 组件骨架：命令契约 + tmc 报警 + HMI 三段式 |
| device | `spt-cylinder-single/-dual/-feedback` | 独立 | 气缸三档（单阀/双阀/带反馈），依赖链自动递归 |
| device | `spt-digital-output` / `-input` | 独立 | 数字量 IO 组件（`FB_Init` 注入硬件引用） |
| communication | `tcpip-client` | 独立 | TCP 客户端，自动重连 |
| tools | `blink` | 独立 | 方波/心跳 |

SPT 系模板取自 `reference/SPT_V4_Samples-main/` 的官方 V4 样例（真工程代码，
非文档散文），设计依据见 `G:\claude\SPT\SPT_Application_Framework_Complete.md`。
用它们前要先装 SPT 库（PLC → Library Repository → 加本地 repo 位置）。
