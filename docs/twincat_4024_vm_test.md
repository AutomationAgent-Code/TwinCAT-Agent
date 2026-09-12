# TwinCAT 4024 虚拟机兼容性验证

## 目标

主机保留现有 TwinCAT 环境，4024 只安装在 Windows 虚拟机中。主机生成
`TwinCAT-Agent-4024-TestKit.zip`，虚拟机负责环境体检、安装和真实 XAE 回归。

4024 候选扩展使用 32 位 XAE Shell 15、.NET Framework 4.7.2 和 x86 WebView2 Loader。
后端仍是独立 64 位便携 Python，通过 COM ROT 连接同一普通用户会话中的 XAE。

## 准备虚拟机

1. 建立 Windows 10/11 64 位虚拟机并创建快照。
2. 使用 Beckhoff 官方 4024 安装介质安装 TwinCAT XAE Shell。
3. 安装 .NET Framework 4.7.2 或更新版本。
4. 安装 Microsoft Edge WebView2 Evergreen Runtime。
5. 将测试套件解压到虚拟机本地目录，不要直接从共享压缩包运行。

## 安装前体检

在测试套件目录中以普通用户打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Test-TwinCAT4024Environment.ps1 -Strict
```

必须看到 `Compatible: True`。脚本只读取文件版本、PE 架构、.NET 版本、
WebView2 和 COM ProgID，不修改注册表、TwinCAT 配置或路由。

## 回归矩阵

1. 完全关闭 XAE，双击 `TwinCAT-Agent-4024-Setup.exe`安装。
2. 用普通用户启动 TwinCAT Agent，不要以管理员运行。
3. 打开 XAE，单击一次 TwinCAT Agent 菜单，确认面板出现。
4. 停靠、浮动和拖动 XAE 窗口，确认面板不白屏。
5. 输入 4024 虚拟机自身的 TwinCAT System ID 对应授权码。
6. 配置一个 Provider，测试普通对话、停止和继续输出。
7. 打开一个新解决方案，测试 `tc_connect_check`、`tc_project_info`和 `plc_structure`。
8. 新建测试 PLC 项目，测试创建 POU、写入代码、读回代码和编译。
9. 切换两个解决方案，确认当前项目和历史对话各自独立。

普通虚拟机适合验证 XAE 面板、COM Automation Interface、项目编辑和离线编译。
Config/Run、XAR 实时性、真实 EtherCAT 扫描和网卡绑定需要额外的虚拟化/硬件条件，
不能因为离线 XAE 回归通过就宣称这些功能已通过 4024 认证。

## 失败时收集

- 体检报告：`TwinCAT4024-Environment.json`
- Agent 日志：`%LOCALAPPDATA%\Programs\TwinCAT Agent\_backend.log`
- XAE 活动日志：
  `%APPDATA%\Beckhoff\TwinCAT\3.1\ComponentConfig\AppEnv\15.0\ActivityLog.xml`
- 虚拟机 Windows 版本、TwinCAT 完整版本号、虚拟化平台和复现步骤。
