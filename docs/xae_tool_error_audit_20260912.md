# XAE 对话工具错误审查（2026-09-12）

## 核实范围

- XAE：PID `14516`，窗口 `TwinCAT Project2 - TcXaeShell`。
- 解决方案：`C:\Users\Aurora Home Office\Desktop\TwinCAT Project2\TwinCAT Project2\TwinCAT Project2.sln`。
- 以该解决方案 `.TwinCATAgent/agent.db` 只读查询为证据；最新任务记录同样绑定 PID 14516 和此解决方案。
- 最新对话 ID：`431e8d86adf04fc08ce6a373883f636b`。
- 最新执行 ID：`7d03171b385d49cfba66d8afa9d24f7a`（用户“确认授权”，15 次模型响应）。
- 未重放写入、编译或运行时动作；未修改工程、历史、配置或运行中的后端。

## 发现与因果边界

| 现象 | 实际证据 | 结论与约束 |
| --- | --- | --- |
| `plc_patch` 无效对象路径 | `plc_read_smart` 的 `source=disk_index` 返回 `Untitled1^POUs^MAIN.TcPOU`；模型直接用作 patch.path 被拒绝。`plc_find` 返回完整 `TIPC^Untitled1^Untitled1 Project^POUs^MAIN` 后修改成功。 | 索引路径和 COM 路径共用 path 字段，旧提示词还要求原样传给写工具，是接口语义及策略冲突。索引路径不得当写入路径。 |
| 编译失败但无可定位代码诊断 | `failedProjects=2`、`compiler_verified=false`；错误为前一天 Modbus/TcpIp 无许可证日志，file 为空、line=0；另有当前 Untitled1 build failed 消息。 | 失败事实成立，但消息不能证明本次编译因许可证失败，也不能排除代码错误。需要本次编译器诊断；不能猜测代码修复点或推荐禁用服务。 |
| `plc_verify` 被阻止 | 顺序为 `plc_build` → `plc_diagnostics` → `plc_verify`，期间没有源码进展；返回 no source progress or three repair rounds exhausted。 | verify 包含编译，换入口不能解决诊断缺失。保护不应关闭；先查明诊断或报告阻塞。返回信息未区分具体停止分支，不能断言本轮真的执行了三次修复。 |
| 最终回复宣称语法正确、可部署 | 最后仅 TCSA 0 错误、5 警告；编译仍失败、verify 被拦截，但对话状态为 completed。 | TCSA 与编译、在线验证不同；结论越过证据。需要完成证据层强制约束，不能只依赖提示词。 |

读取结果还标记 `dirty_unknown=true`，部分正文被摘要。写前必须精读当前目标区，不能把已保存索引当作未保存编辑器内容，也不能凭历史摘要构造替换正文。

## 本轮落实与分工

本轮新增 `tc_agent/tool_usage_contract.py`，作为 CLI、XAE 前台和后台工作任务共用的模型调用规则。删除 backend 原有“catalog.path 直接传写工具”的矛盾指引。规则覆盖：

1. 参数校验错误与已执行/不确定写入分开恢复。
2. 索引路径、COM 对象路径、ADS 符号名分离。
3. 已保存/未保存、完整/截断内容分离，写前精读。
4. 编译失败按诊断来源、时间和位置处理，不盲改、不换验证工具绕过保护。
5. 桥接成功、源码回读、编译、TCSA/TE1200、在线断言分别报告；失败编译不能宣称可部署。
6. 在线断言不符不等于连接故障，不通过反复写值或改预期制造通过。

新增 `tests/test_tool_usage_contract.py` 验证两个提示词入口、关闭质量门禁时仍保留规则、路径冲突指引移除及错误类别约束。

底层修复由“任务执行者”接手：索引结果路径身份、无源码定位的 build 诊断分类、完成证据/最终状态漏检。软件包由“TwinCAT Agent Package”协调纳入。此文不是底层修复已完成、安装版已更新或本次 PLC 编译根因已找到的声明。
