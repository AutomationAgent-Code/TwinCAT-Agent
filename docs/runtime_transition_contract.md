# Runtime 操作契约

## Agent 请求范围与动作表述

明确的读取/诊断请求在前台和后台任务执行门禁处拒绝变更工具，不因 auto 模式或旧任务上下文而扩大到登录、启停或切平台。
这是保守的请求模式识别，不是任意自然语言授权解析器；其他请求仍沿用原审批门禁。
登录回读 ProgramLoaded 且 Run 后，Agent 应停止调用 Start，刷新仅用只读检查。
底层仍在操作前读取实际状态，不用上一轮状态缓存替代在线验证。
每阶段返回 command_sent；already_satisfied 必须报告为“原本已运行，跳过启动”。

## 2026-09-08 目标平台来源与编译前检查

最新实现优先通过本机 Beckhoff Target Browser 随附的
`TwinCAT.SystemService.Commands.GetDetailedDeviceInfoCommand` 按实际 XAE Target NetId
读取 `DetailedTargetInfo.Platform`、操作系统和 CPUArchitecture，不更改路由。
本机实测目标返回 `TwinCAT RT (x64)`。旧的 CPUType=86 推断、其他值猜 ARM 和默认 x64 均不再作为自动选择依据。
库不存在或目标读取失败时保持 unavailable，不能用本机系统或当前下拉框代替目标证据。

`tc_platform_set` 默认保持 Debug/Release；空 full 只预览目标唯一匹配候选，不取第一项。
目标已明确返回的平台不允许通过 acknowledge 绕过不匹配；无法识别时需硬确认。
`switch_verified` 与 `target_match_verified` 分开返回；切换前后校验上下文及工程映射。
PLC build/online 在进入实际执行通道前要求匹配目标平台；HMI build 要求 TwinCAT HMI 平台及映射。
编译前检查不自动切平台、不编译、不启停；错误结果提供 required_full 供确认切换后重试。
HMI 构建不查询 PLC 目标。本轮没有进行真实切平台或编译验收。

官方替代读取入口：
[Get-TcTargetInfo](https://infosys.beckhoff.com/content/1033/tc3_ads_ps_tcxaemgmt/11224362507.html)。
直接 ADS 200/0x700/4 在本机返回 1794，不能用其失败推断所有目标信息通道均不可用。
下文早期关于 target_architecture_verified=false 的说明以具体命令返回的字段为准。

系统 Run、PLC Run、XAE 登录以及当前程序身份是独立证据，不得相互替代。

- state：读取工程实际目标的 ADS System Service 10000；失败不以 TIRS/IsTwinCATStarted 代替。
- activate：默认仅提交配置；CLI `--restart` 显式组合重启。请求失败不标记 configuration_submitted。
- restart：请求成功后轮询 System Service Run；请求失败不能被已有 Run 状态掩盖。Run 回读不证明新配置版本已加载。
- login/logout/start/stop/online：CLI、MCP 和 Agent 共用 runtime_contract。多 PLC 必须明确 runtime 或 all_plcs；无 PLC 返回 skipped、verified=null。CLI 未验证成功返回非零退出码。
- login：系统必须 Run；读取 NestedProject.ProduceXml 的 OnlineSettings，等待 LoggedIn=true 且 PlcOpState=ProgramLoaded。没有字段、下载待确认或超时均停止，不自动确认弹窗。
- logout：等待 LoggedIn=false；不要求系统 Run，也不停止 PLC。
- start/stop：必须确认 IDE 已登录且 ProgramLoaded；再按工程读取的实际 ADS 端口验证 Run/Stop。不得默认端口 851。
- online：不强制 logout；依次执行确认登录/加载、Start、实际端口回读。已达到目标状态则不重复发命令。某 PLC 不确定时停止处理，并列出尚未处理的 PLC。
- 每阶段前复核解决方案、目标 NetId 和所选 PLC 端口，发现改变则阻止后续操作。不自动切 Config、切目标或激活。

## PLC 在线平台门禁

login/online/start/stop 在访问运行时前通过 DTE 读取当前解决方案平台和实时工程映射。
`TwinCAT HMI`、未知平台或缺失映射返回 `blocked` 和 `platform_*` 错误码，不发送在线命令。
TwinCAT 系统/PLC 工程映射必须与当前 TwinCAT RT/OS 平台一致；HMI 工程的独立映射不参与比较。
流程各阶段前再次核对平台，用户中途切换后不继续 Start。logout 不要求切回 PLC 平台。

错误结果提供 `platform` 和 `next_action`：先列出平台，明确选取与目标匹配的平台，回读映射后重新编译/登录。
工具不自动切平台、不保存工程、不借此扩大到编译或部署。此门禁不替代实际目标架构识别：
`target_architecture_verified=false`，不按 CPUType=86 推断 RT/OS 或位数，也不把未知 CPU 猜成 ARM。

## 证据边界

LoggedIn/ProgramLoaded 证明在线通道和程序已加载，不证明加载的是刚编译的本地代码，因此 application_identity_verified=false。部署另行比较目标应用身份，不能仅凭 PLC Run 宣称部署成功。配置加载身份、4024/4026 各过渡状态的完整语义仍需专项验证。

新版 online 可以在真实登录证据满足后自动 Start；与旧版固定等待不同，证据缺失时 fail closed。原生与 PowerShell 都实现状态读取；自动后端优先 PowerShell，避免旧缓存 helper 不认识新命令或忽略多 PLC 选择。

验证分两类：离线回归覆盖拒绝命令、未完成登录、多个端口、目标变更及 CLI 退出码；当前 XAE 只读验证不等于已经测试实际登录、下载或启停。源码更新不等于已更新安装程序。
