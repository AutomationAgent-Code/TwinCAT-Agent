# PLC Build/Rebuild 执行契约

Agent 的 `plc_build` 和 `plc_verify` 是效果型工具，不再因为返回了诊断而被当作只读调用自动放行。执行前必须先调用 `plc_build_status`；该只读工具从当前 XAE 读取解决方案、PLC 工程范围、PLC 登录状态、DTE 的 Build/Rebuild 实际命令可用性和构建占用，并为实际可用的动作签发短时 `build_plan_token`。

构建调用必须把 `action` 与对应令牌原样交给审批门。审批通过后，Agent 再次读取同一 XAE 的状态，并比较 PID、解决方案、工程范围、登录状态、命令可用性、占用状态和状态指纹；任一项变化都返回 `status=conflict`、`not_executed=true`，不调用 Build/Rebuild。令牌只使用一次并在短时间后失效。

Build 与 Rebuild 的选择来自 XAE 实际 DTE 命令状态，不把 PLC Online 状态或某个 TwinCAT 版本的经验硬编码成 Rebuild 规则。当前状态不可读、命令不可用或构建占用不明确时，不签发令牌。

构建后会再次读取状态，并将编译诊断与状态验证分开报告。XAE 管道在请求已写入后如果丢失响应，返回 `status=uncertain`；桥接层禁止回退到第二次 COM 编译。构建流程不会自动 Logout、Stop、Start、Activate、切换模式或修改 PLC 代码。

`plc_diagnostics` 与 `plc_preflight` 仍是只读工具，不会因为构建审批门而触发编译。真实 XAE 构建验证需用户在本轮明确批准对应的 `action` 和 `build_plan_token` 后进行；源码回归测试不等同于真实构建验证。
