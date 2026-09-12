# TwinCAT Agent 测试策略

本文定义测试分层、环境、pytest marker、质量门禁和证据要求。测试数量会随产品变化，唯一有效的通过基线是对应提交在受控 CI 或指定实验室环境生成的结果，不能用文档中的固定数字代替。

## 1. 测试分层

| 层级 | 内容 | 运行时机 | 是否可证明真实 TwinCAT 兼容性 |
|---|---|---|---|
| L0 静态检查 | 配置、清单、版本一致性、安全与依赖扫描 | 每个 PR | 否 |
| L1 单元测试 | 纯 Python、mock/fake COM、CLI 参数和错误契约 | 每个 PR | 否 |
| L2 组件集成 | 后端、WebSocket、SQLite、浏览器 UI、构建产物冒烟 | PR 或合并后 | 否 |
| L3 XAE 集成 | 真实 XAE 中的 COM 连接、创建、读写、编译、关闭后重开 | 每日或受控 runner | 只能证明对应 XAE 环境 |
| L4 Runtime 集成 | ADS、激活、重启、登录、启动、应用身份校验 | 每日实验室测试 | 只能证明对应 Runtime 环境 |
| L5 硬件在环 | 路由、EtherCAT 扫描与链接、I/O、NC 和故障恢复 | 夜间或发布前 | 是，仅限记录的设备矩阵 |
| L6 发布验收 | 干净机安装、升级、卸载、签名、哈希和人工探索 | 发布候选 | 取决于随附的 L3-L5 证据 |

mock、源码文本断言和离线编译不能替代真实 XAE、Runtime 或硬件证据。没有完成 L4/L5 时，结论必须写成“离线兼容”或“XAE 集成通过”，不得扩大为硬件生产就绪。

## 2. 环境矩阵

| 环境 | 最低测试内容 |
|---|---|
| Windows CI，Python 3.9 与主线版本 | L0-L2；安装项目后完整收集并运行离线测试 |
| Windows 10/11，TwinCAT 3.1.4024，Shell 15 x86 | 32 位桥、PID 绑定、POU 创建/读写/编译/重开、WebView2 面板 |
| Windows 11，TwinCAT 3.1.4026，VS 17 x64 | 64 位桥、同一套 COM 契约、WebView2 面板 |
| 本地 XAR | ADS 端口发现、多 PLC、activate/restart/login/start/stop、build identity |
| CX x64 与适用时的 ARM 控制器 | 路由、平台检测、部署和断网/重启恢复 |
| EtherCAT 台架 | 至少主站和 DI/DO；运动测试另需受控伺服台架、急停、限位和机械隔离 |

每次报告必须记录 Windows、TwinCAT 完整版本、XAE PID/位数、Python/桥版本、目标型号与固件、AMS NetId（对外报告需脱敏）、EtherCAT 拓扑和测试提交 SHA。

## 3. pytest marker

项目启用 `--strict-markers`。新增环境相关测试必须使用 `pyproject.toml` 中注册的 marker：

- `unit`：无 TwinCAT、硬件或外部服务的隔离测试。
- `integration`：进程或组件集成测试，不要求物理 TwinCAT 硬件。
- `ui`：浏览器、WebView2、视觉或无障碍测试。
- `xae4024`、`xae4026`：分别要求对应真实 XAE。
- `runtime`：会改变或验证 TwinCAT Runtime 状态。
- `hardware`：要求控制器、EtherCAT 或运动硬件。
- `destructive`：可能改变 Runtime、目标、路由、I/O 或设备状态，必须显式批准。
- `slow`：不适合 PR 快速反馈环的测试。

常用命令：

```powershell
# 默认离线套件；CI 应安装项目后执行
python -m pytest

# 明确排除实验室与有状态测试
python -m pytest -m "not xae4024 and not xae4026 and not runtime and not hardware and not destructive"

# 4026 XAE runner
python -m pytest -m "xae4026 and not destructive"

# 实验室有状态测试：操作人必须先确认目标与恢复方案
python -m pytest -m "runtime or hardware" --maxfail=1
```

仅添加 marker 不会自动隔离测试。CI 或实验室入口必须使用明确的 `-m` 表达式；`destructive` 测试不得进入无人值守的普通 PR job。

## 4. 质量门禁

### Pull Request

- 测试收集无错误，受影响测试及离线套件通过。
- 新增或改变的行为有对应回归测试；错误路径不得返回假成功或错误的零退出码。
- 不降低已批准的覆盖率基线，不引入高危秘密、依赖或安全扫描结果。
- 涉及 UI 时完成关键交互、键盘操作和严重级无障碍问题检查。

### 合并与发布候选

- 组件集成、wheel、VSIX 和适用的安装包构建通过。
- UI 单一真源、`VERSION`、安装包版本资源和 SHA256 一致。
- 4024/4026 或硬件相关改动必须在受影响环境重测；涉及 Runtime、I/O、路由或 NC 的改动必须有相应 L4/L5 证据。
- P0/P1 缺陷为零。P2 只有在负责人、截止日期、绕过方案和风险接受均已记录时才能豁免。
- 不得把 API Key、私钥、客户授权、聊天历史或项目私密数据打入制品。

## 5. 证据与结果报告

自动化结果至少保存：提交 SHA、完整命令、环境清单、收集数、通过/失败/跳过数、退出码和日志。发布结论引用 CI 链接或归档报告，不手工抄写长期“通过数基线”。

真实 XAE、Runtime 和硬件测试还必须保存：

- 测试前后的目标、Runtime 和工程状态；
- 关键步骤的日志或截图，以及编译错误列表；
- PID 绑定、桥位数、ADS 状态和应用身份校验结果；
- I/O/NC 测试的拓扑、互锁、审批人与恢复结果；
- 失败的最小复现步骤、时间戳和必要的诊断文件路径。

跳过测试必须带明确原因。环境缺失、设备未到或许可证不可用属于“未测试”，不能记录为通过。涉及目标或硬件的测试结束后，应按预先批准的恢复方案还原测试工程和链接；不得直接修改注册表、`StaticRoutes.xml` 或删除控制器文件。
