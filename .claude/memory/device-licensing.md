# TwinCAT Agent 设备授权

- 客户端模块：`tc_agent/licensing.py`，纯标准库 RSA-SHA256 验签。
- 授权码：`TCAG1.<payload>.<signature>`，仅绑定主板固件标识派生的
  `TCA-...` 设备码，支持客户名、授权编号和到期时间。优先使用
  `Win32_BaseBoard.SerialNumber`；若厂商返回 `Default string` 等占位值，
  回退到 `Win32_ComputerSystemProduct.UUID`。不使用 MachineGuid/硬盘/网卡；
  重装 Windows 不变，更换主板后需重新授权。
- 未授权时后端只处理 `activate_license/get_license_status`，不会探测 XAE、
  读取历史、加载 Provider 或执行 Agent。
- UI：`tc_agent/static/index.html` 的全屏授权门禁。
- 客户端授权记录：`%LOCALAPPDATA%\TwinCAT Agent\license.json`。
- 供应商发码工具：`scripts/license_issuer.py`（依赖开发机 `cryptography`）。
- 供应商桌面 UI：`scripts/license_issuer_ui.py`；构建脚本
  `scripts/build_license_tool.ps1`；交付物
  `dist/TwinCAT-Agent-License-Issuer.zip`。GUI 包不包含私钥。
- 产品私钥位于仓库外：
  `G:\claude\TwinCAT-Agent-License-Secrets\private_key.pem`。
  私钥绝不能提交、打包或发给客户；必须离线备份。
- 完整发码说明：`docs/licensing.md`。
