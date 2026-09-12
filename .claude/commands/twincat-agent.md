# TwinCAT Agent

项目开发编排技能：按用户意图选择模板、PLC 编程和平台操作，不默认执行全链路部署。

## Trigger

`/twincat-agent <request>`

## 任务边界

- 查看、解释、审计：读取并给出证据，不写代码、不切目标、不部署。
- 创建或修改：确认 XAE 会话与对象，完成限定范围的变更、回读和编译；不自动扫描硬件、登录、启动、激活或重启。
- 用户明确要求上线或部署：确认实际 Target NetId、PLC 项目和运行时端口，再按对应路径执行已有审批流程。已有明确授权不重复询问；目标不明或操作超出授权时先确认。
- 用户提到某个设备名称/IP，不等于要求切换目标或添加路由。只读排查也不要求关闭其他 XAE 会话。
- 在线变量读取与在线写值分开；注入测试条件、启动机械动作需要相应授权。编译通过不证明在线行为正确。

## 按需加载子技能

| 任务 | 指令文件 |
|------|----------|
| 项目模板的选择、创建、提取、验证 | [模板管理](twincat-template.md) |
| PLC 对象、代码、成员、库管理 | [PLC 编程](plc-programming.md) |
| 构建、路由、I/O、NC、部署与运行模式 | [平台控制](twincat-platform.md) |
| 编写或修改 PLC 代码前 | [编码规范](plc-coding-standard.md) |

只读取与当前任务有关的子技能。命令参数以当前 CLI 的 `--help` 或工具 schema 为准，不能把 slash 入口直接当作 shell 命令。

## 编程和读取策略

1. 修改前读取实际对象；用当前 XAE PID、解决方案及工具返回的精确树路径定位，不用第一个 DTE/Projects.Item(1) 猜目标。
2. 写 FB 前先 `fblib find "<功能>"`（Agent：`fblib_find`）；命中则 `fblib add` 后改。未命中再按项目编码规范生成骨架/实现，不能混用 SPT 和 standalone 的约定。
3. 已打开项目通过 COM 高层封装读写；离线项目才使用文件系统 XML。禁用鼠标坐标、剪贴板模拟输入 PLC 代码。
4. 安装版读取全项目先 `plc_source_catalog`；读取整个 FB 按成员批量 `plc_read_fast`，避免逐个重复 read。已保存源码默认走索引；未保存内容、写后验证走 live 或当前页工具。不要把缓存读当实时读。
5. 成功静态读取可复用；写入后回读、实时变量变化、失败后的合理重试不受静态去重限制。工具提示结果过大时缩小对象/成员/行范围，不盲目增大返回长度。
6. 创建顶层对象用 `plc_create`；已有 FB 的方法/动作使用 `plc_create_member`，属性使用 `plc_create_property`，不要重建父 FB。底层参数详见 PLC 子技能。
7. 写入失败或中断可能已部分生效；先回读再决定重试。编译失败不能继续上线；错误来源不可用时明确报告未验证，不把空列表说成 0 错误。

## 工作流示例

### 只创建项目

用户：“用某模板创建一个新工程。”

核对模板存在、名称和输出目录 → `tc-template create <tpl> -n <name> -o <dir>` → 检查创建结果。
不附加设备发现、添加路由、硬件扫描、NC 轴配置或部署；模板缺失时先列出实际模板，不假定固定数量。

### 只修改程序

用户：“修改 MAIN 的计数逻辑。”

定位会话与 MAIN → 读取声明/实现 → 按规范局部修改 → 回读 → `tc build` → 报告验证结果。
到这里完成；未获上线授权时说明在线行为尚未验证。

### 修改后上线（用户已明确要求）

仅代码变更且不涉及 I/O 映射、地址分配或 NC 配置：构建成功后 `tc online`（Login + Start）。
按实际 PLC endpoint 读取关键变量，与预期值比较；不能仅凭运行状态 Run 宣称逻辑正确。

### 全量部署（用户已明确要求）

配置变更需要部署：确认目标及影响 → `tc deploy`。
底层阶段为 build → boot → ActivateConfiguration → restart → login → start；无 PLC 项目时跳过 PLC 专属阶段。
CLI `tc activate` 已包含激活和重启，不要再机械追加一次 `tc restart`。
部署不隐式切 Config；只有明确要求切模式或授权的硬件扫描确实需要时才使用 Config。

### 添加指定路由

用户：“给 CX-New 的 192.168.1.100 添加路由。”

`tc target add -n CX-New -a 192.168.1.100 --auto-auth`

`--auto-auth` 使用默认凭据 Administrator/1；自定义凭据使用 `--user` / `--password`，不得输出密钥。
通过封装的路由工具操作，不手改注册表或 StaticRoutes.xml。添加路由不等于授权切目标或部署。

## 安装与运维边界

前端嵌入遵循 [XAE 固定契约](../../docs/xae_embedding_contract.md)，不得改成 VSIXInstaller 注册或覆盖 Beckhoff 官方扩展。
后端运维参考 [当前运维指南](../memory/coagent-backend-operations.md)；保留项目 SQLite 历史，不通过清空会话恢复。

## 指令来源与维护

本文件和同目录子技能是开发助手的 slash 指令，安装版不会自动加载 `.claude/commands`。
安装版模型策略由 `tc_agent/backend.py::_system_prompt` 与 `tc_agent/coding_profile.py` 生成；
工具权限由执行层落实。修改任务边界、验证或编码策略时同时检查运行时提示及测试，再按需同步安装版。
实现入口：`tc_template/cli.py`（命令参数）、`tc_template/plc.py`（COM PLC 封装）、
`tc_template/tc_platform.py`（平台控制）。不要引用已移除的编辑模块。
