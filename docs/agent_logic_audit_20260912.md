# Agent 对话、审批、执行与验收链路审查

## 范围与方法

本轮只审查，不修改产品实现、配置、会话数据库或 PLC，不发布安装包。
读取当前 XAE PID **19584**（已不是旧 PID 14516）的 connect-check/build-state，
核对解决方案 `C:\Users\Aurora Home Office\Desktop\TwinCAT Project2\TwinCAT Project2\TwinCAT Project2.sln`。
后端 PID 14752，版本 1.0.8.108。SQLite 以 mode=ro 查询。
对授权解析、构建结果合并和身份校验使用内存 mock 复现，未真实 Build/Rebuild。

当前对话 `431e8d86adf04fc08ce6a373883f636b` 最近三轮：

- `42d70d419f75448cabe68551d64884ce`：在线行为验证，生成自然语言使能/读取/恢复计划。
- `4f10d509de954207b1b22485844a9f9d`：确认得到的却是 tc_login 计划，写值被拒绝。
- `06308e5f5d25497b86c539566ebdde50`：单独审批使能成功，后续恢复被拒绝。

`build_execution.py`、`authorization.py`、`backend.py`、`xae_build_pipe.py` 的源代码与安装 app 文件 SHA256 相同；下述问题不能一概归咎于未同步旧后端。

## 已确认问题

### P0：测试已改变设备状态，恢复缺失且中断报告掩盖已执行步骤

最新 run 中 `plc_write_value(MAIN.bEnable, True, expected_before=False)` 有独立批准记录，
实际 written=true，readback=true。随后写 False 被授权层拒绝。
最后回复只有“权限未获确认，动作未执行”，没有说明先前已使能、恢复未完成。
本轮只读 ADS 回查：`MAIN.bEnable=true`，NetId `192.168.1.4.1.1`、端口 851。
这不是未授权使能的结论，而是**计划不完整与部分执行状态报告失真**。

证据位置：backend.py 的 authorization_blocked → stop_reason 分支和直接返回路径。
该路径没有输出副作用台账，亦未进入正常最终回复的验收处理。
必须先让用户明确看到剩余设备状态；不允许为掩盖故障自行执行未批准的恢复。

### P1：自然语言计划被错误转换成不同的机器授权计划

对话明确列出 `bEnable=TRUE → 读取 → bEnable=FALSE`，但授权兼容函数
`explicit_report_actions` 仅用关键词匹配，将“已登录且处于 Run”识别成 tc_login。
直接用数据库中的原文调用该函数，复现结果仅为 `[tc_login(args={})]`。
它不解析表格中的写值动作。持久化计划 `auth-5ebefaf49092421283b2a9d10abc16cf` 也只有 Login。

应取消从叙述推断执行许可，统一由结构化计划产生用户看到的步骤，UI 审批必须展示同一份内容。

### P1：单动作计划完成后仍约束整轮，后续动作无法重新申请批准

第二次审批只批准 TRUE，完成后 active_authorization_plan 仍指向 completed 计划。
backend 的“有 active plan 且动作不属于剩余清单”分支在普通审批入口之前拒绝 FALSE，
因此没有给用户批准恢复的机会。
正确方案不是放开所有写入：测试启动前明确批准完整的准备、激励、观察、恢复步骤；
新增动作需要新审批，已完成计划不得被当成整轮永久拒绝规则。

### P1：构建封装篡改底层执行事实

内存 mock 让 com_build 返回 blocked、buildPerformed=false、not_executed=true，
execute_plc_build 却返回 buildPerformed=true 与 not_executed=true 同时存在。
原因是结果合并无条件把两个 build_performed 字段设为 True。
底层 uncertain 也不能被上层改成确定已执行。执行状态必须单向、保真传播。

### P1：构建身份检查允许不同解决方案的证据拼接

mock connect-check 为 A.sln、build-state/project-info 为 B.sln，
capture_build_state 仍返回 ready=true、verified=true 并签发令牌。
identity_ok 只判断字段存在，没有核对每个来源的 solution 是否一致；也不完整核对期望 PID。
指纹含错误拼接后的状态不等于身份已验证。

### P1：审批到实际构建之间仍有竞争窗口，构建后也未严格比较身份

状态捕获发生在锁获取和 com_build 平台预检之前；命名管道只传 command，
没有把批准的 solution/配置/源版本传给 XAE UI 线程验证。
构建后的 capture 主要检查 ready/verified，不比较 before/after 的 PID、solution、项目集合。
应在最终宿主执行点原子核对批准身份；未保存/源 revision 亦尚未纳入构建指纹。

### P1：Rebuild 的“完成”仍可能是上一次 Done

当前 XAE 实测命令状态 Build=false、Rebuild=true，符合用户观察。
VSIX 源码仍只接收 build/diagnostics；rebuild 依赖客户端收到 unsupported 后转到 COM。
因此不能将其表述成“VSIX 已原生支持 Rebuild”。
native bridge 的 RebuildSolution 是异步命令，随即看到 BuildState==3 就结束等待，
没有本次开始/结束事件关联，可能读取旧 LastBuildInfo；等待耗尽也没有严格的本次完成证明。
需要 request_id + BuildBegin/BuildDone 或等效本次完成证据，不能拿旧 Done 充数。

### P1：完成证据是本轮内存对象，不是跨轮、按源码版本的工程事实

每轮新建 CompletionEvidence。上一轮编译失败不能可靠约束下一轮“在线验证”结论。
源码版本、构建产物、目标在线应用身份没有统一关联。
“能读到变量”“应用 Run”不能证明运行的是当前修改版本。
应持久化版本化证据，代码变动使旧构建证据过期；目标应用身份未知则明确标记未知。

### P2：正则验收不能承担最终可信性门禁

新实现仍漏掉原始错误句“代码已准备就绪，可部署到目标系统。”。
直接调用 completion_claims 返回 []；全新 CompletionEvidence.guard_final 原样放行。
依靠不断增加中文措辞的正则不能覆盖同义句，也不能证明否定/引用语境正确。
最终状态应由结构化证据生成，模型只负责解释，不负责决定通过与否。

### P2：返回协议与内部辅助函数契约仍自相矛盾

- verify_build_plan 的注释说不消费令牌，实际调用 _claim_plan 会消费。
- 缺少令牌的 next_action 竟指示“先调用 plc_build，再调用 plc_build_status”，顺序相反。
- capture_build_state 在执行前/后也签发新令牌，_PLANS 无过期清理，容易积累无用状态。
- runtime 选择只筛选登录状态，而执行仍是 SolutionBuild；UI 必须明确全解决方案范围，不能表现为仅构建所选 PLC。
- 同参数重复动作使用 key 集合消费，不能表达“先写一次、稍后再次写相同值”的步骤身份。

## 测试与交付机制的问题

现有构建测试把 com_build 完全 mock 成成功，验证“被调用一次”不等于验证 XAE 真正
执行了一次，也没有覆盖旧 Done、混合解决方案、底层阻断/不确定状态透传。
授权测试缺少此次自然语言计划→持久化计划→审批→使能→恢复→中断汇报的贯通场景。
全量单元测试通过、安装导入成功、HTTP=200，均不能作为真实操作闭环已通过的证据。

协调交付方面：本对话此前直接转述任务执行者“已完成”报告，没有逐项独立验收，
并连续追加任务，导致责任、完成范围与安装包验收边界不够清晰。应以一个固定问题清单
追踪 owner/status/evidence，当前任务完成后接下一个；未验证能力不得包装成完成。

## 建议的修复顺序（本轮未实施）

1. **先封住部分执行失真**：所有终止路径展示已执行副作用、最后回读值、未执行恢复动作；测试计划完整审批后才开始激励。
2. **统一审批状态机**：显式 plan_id/step_id、完整参数、作用域和恢复步骤；移除自然语言自动提取执行权限；恢复不是无条件自动动作。
3. **统一执行结果协议**：not_started / running / succeeded / failed / uncertain，结果不能被包装层改写；预览令牌与用户授权分开。
4. **修复构建事务边界**：宿主内原子复核身份、命令能力、构建占用；关联本次开始/结束和诊断；旧扩展能力明确降级。
5. **版本化验收与上下文**：摘要只帮助理解，执行台账/待办/源码版本/构建证据才决定能否继续和是否完成。
6. **建立事故回放验收**：先用 mock/隔离测试覆盖上述事故，再在用户明确批准且设备安全的环境验证；最后冻结打包，不边修边更新。

本轮覆盖对话编排、权限、执行台账、PLC 构建、证据、摘要入口及交付核对；
不是对全部 206 个工具、所有 HMI/Safety/外部 MCP 路径逐项完成实机验收。
