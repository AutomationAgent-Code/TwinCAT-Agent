# 用户要求：编译与代码修改须离线

离线指 XAE PLC 编辑器 logged_in=false，不是 ADS Stop 或 TwinCAT Config。
源码修改公共门禁按 TIPC 父 PLC 项目查询登录状态；未提供精确项目范围时核对全部
已发现 PLC，未知状态不放行。审批、dirty/source baseline 门禁保持独立。

Build/Rebuild 当前为解决方案级命令，签发和执行构建计划前检查全部 PLC 登录状态，
不因调用者填写一个 runtime 而遗漏其他在线 PLC。任何在线工程均不签发构建计划。
先申请明确的 tc_logout 审批；登出后重新读取代码基线/构建计划，不能复用旧证据。
不自动 Stop、切 Config、激活、下载或重启。只读源码及诊断工具不要求登出。

这是用户选择的保守工作流，不声称 TwinCAT 本身禁止在线编辑或所有在线构建。
