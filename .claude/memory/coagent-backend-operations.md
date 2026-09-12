---
name: coagent-backend-operations
description: TwinCAT Agent 当前后端运维：精确会话绑定、项目 SQLite、Provider 配置和安全更新
metadata:
  type: reference
---

# TwinCAT Agent 后端运维

文件名保留以兼容旧链接。历史 Claude SDK/CLI resume 故障不再是当前架构的诊断依据。

## 当前数据与服务

- `tc_agent/backend.py` 承接会话，`tc_agent/agent_core.py` 通过 HTTP 调用模型。
- `8765` 提供聊天 WebSocket，`8766` 提供页面及 HTTP 接口。
- 项目对话数据库位于解决方案目录的 `.TwinCATAgent/agent.db`；无项目会话使用后端模块目录的 `agent.db`。
- JSON 历史适配器仅为迁移/兼容保留；不要清空旧 session_id、删除 SQLite 或 WAL/SHM 来“修复上下文”。
- 模型端点、模型名、凭据和代理以 Agent 自己的 Provider 配置为准，不依据模型自报身份或机器上的 CC Switch 配置判定。

## 排查顺序

1. 先确认用户实际安装目录、页面绑定的 XAE PID、解决方案路径与当前对话，避免把开发版或另一个 XAE 当现场。
2. 查看 8765/8766 的监听进程、可执行路径和命令行。端口占用本身不能证明是旧 Agent，PID 0 不是可终止的后台进程。
3. 安装版启动器的 `_backend.log` / `_backend.out` 用于定位异常、取消、连接失败和 Provider 错误；按实际启动目录找日志，不拿另一次启动的日志下结论。
4. 检查项目数据库绑定、当前轮次和工具执行记录。中断的写操作可能已经生效，恢复时先回读 PLC，不自动重放写入。
5. 优先使用界面的 Bug 报告或 `agent_report_create` 生成脱敏诊断。默认不携带 PLC 源码；不要输出 Provider key 或整个配置文件。

后端必须普通用户启动，并与 XAE 处于相同权限级别。内嵌页面使用
`http://127.0.0.1:8766/?xae_pid=<当前XAE PID>` 绑定会话；
ROT 不可见时先核对 PID、权限和进程存活，不默认杀掉所有 XAE。

## 更新与重启

- 必读 [会话范围与配置连续性事故](context-and-config-incident-20260907.md)：开发版与安装版配置曾因启动来源切换而看似丢失；用户要求不得重复。
- 重启前后核对实际配置绝对路径、脱敏 Provider 列表和当前选择；仅检查配置哈希及服务健康不足以证明配置连续性。
- 修改仓库不会自动更新正在运行的安装版。
- 需要同步安装版时，使用仓库 `scripts/Update-LocalInstallation.ps1`；仅需后端更新时不要带 `-IncludeExtension`。
- 更新器验证安装路径及后台进程、备份旧文件、保留用户配置和数据库，再隐藏启动后端。
- 更新后核对监听 PID、HTTP 状态和目标模块内容，不能只看复制命令成功。
- 不因占用端口而杀未知进程，不批量终止 Python，不把注册表/缓存删除当作常规恢复。
- 前端嵌入与后端更新分开，前端遵循 [固定契约](../../docs/xae_embedding_contract.md)。

## 网络错误

Provider 请求使用显式代理配置；空 proxy 为直连，有值时走指定代理。
WinError 10061 时核对实际拒绝连接的 endpoint 和 Provider proxy，不直接归因于系统代理。
日志出现超时/认证失败时检查对应 Provider 与请求状态，不重新套用旧版 CLI exit 129 的清会话方案。

架构细节见 [运行时架构](../../docs/agent_runtime_architecture.md)。
