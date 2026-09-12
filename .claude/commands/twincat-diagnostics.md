# TwinCAT Diagnostics

TwinCAT 系统诊断技能 — 全面健康检查、ADS 连通性测试、路由诊断、错误分析、许可证查询。

## Trigger

```
/tc-diag <operation>
```

## 可用命令

| 命令 | 说明 |
|------|------|
| `tc diag health` | 全面系统健康检查 (Runtime/XAE/Solution/Target/Routes/Errors) |
| `tc diag ping [target]` | 测试 ADS 连接到目标 |
| `tc diag route [target]` | 诊断特定路由 (路由存在、ADS 连通、运行时状态) |
| `tc diag target [target]` | 查询目标详细信息 (设备名、CPU 架构、OS 版本、TwinCAT 版本) |
| `tc diag errors` | 读取错误列表并提供增强诊断和修复建议 |
| `tc diag license` | 查询 TwinCAT 许可证状态 |

## 诊断工作流

### 工作流 1: 部署前健康检查
```
User: "做一次系统健康检查"
     ↓
1. tc diag health
   → Runtime:      ✅ State=Run
   → XAE:          ✅ TcXaeShell
   → Solution:     ✅ MyPlc.sln (PLC project found)
   → Target:       ✅ Reachable (2.3 ms via pyads)
   → Routes:       ✅ 3 routes
   → Platform:     ✅ Release|TwinCAT RT (x64)
   → Version:      ✅ 3.1.4024.32
   → Errors:       ✅ 0 items (clean)
   → Overall:      ✅ Healthy
```

### 工作流 2: 排查连通性问题
```
User: "排查为什么连不上目标 CX-12345"
     ↓
1. tc diag ping CX-12345
   → 确认 ADS 是否可达
2. tc diag route CX-12345
   → 检查路由是否存在、ADS 状态
3. tc diag target CX-12345
   → 查看目标 CPU、OS、TwinCAT 版本
```

### 工作流 3: 编译错误诊断
```
User: "帮我看看为什么编译报错"
     ↓
1. tc diag errors
   → 读取错误列表并匹配已知错误模式
   → 提供修复建议
```

## Health Check 检查项

| 检查项 | 说明 | 不健康时的建议 |
|--------|------|---------------|
| **Runtime** | TwinCAT 运行时是否启动 | `tc run` 或手动重启 TwinCAT |
| **XAE** | Visual Studio Shell 是否运行 | 启动 TwinCAT XAE |
| **Solution** | 是否打开了项目 | 创建/打开项目 |
| **Target** | ADS 连通性 (pyads ping) | 检查网线、电源、防火墙、AMS Router |
| **Routes** | StaticRoutes.xml 中的路由数量 | `tc target add --auto-auth` |
| **Platform** | 构建平台是否正确设置 | `tc platform set` 自动检测 |
| **Version** | 本地/目标 TwinCAT 版本 | `tc version pin` 固定版本 |
| **Errors** | 编译错误 (可选) | 根据诊断建议逐一修复 |

## 错误模式匹配

`tc diag errors` 对常见错误提供自动诊断建议:

| 错误关键词 | 建议 |
|-----------|------|
| "not defined" | 检查拼写、作用域、库引用 |
| "ambiguous" | 使用全限定名或加命名空间前缀 |
| "type mismatch" | 检查类型，使用 TO_* 显式转换 |
| "division by zero" | 加 IF divisor <> 0 守卫 |
| "library" | 检查库引用 + 重新扫描 |
| "ADS" | 检查路由 + AMS Router + 防火墙 |
| "license" | 运行许可证管理器 |
| "memory" | 检查 retain/persistent 变量、数组大小 |
| "TMC" | TMC 文件损坏 — 重新扫描库 |
| "access violation" | NULL 指针 — 初始化接口引用、检查 AT% 地址 |

## 实现位置

- **核心诊断函数** (13 个函数, `tc_template/diagnostics.py`)
- **错误列表读取**: `tc_template/inspect.py` — 首选 `dte.ToolWindows.ErrorList.ErrorItems`；Python 3.14 动态绑定下不可用时回退读 Build Output 面板 + `SolutionBuild.LastBuildInfo`。**清列表勿用** `TwinCAT.ClearErrorList`（Analytics 遗留调试弹窗，SilentMode 拦不住）—— `clear_error_list()` 已改为跳过。详见 `/plc` skill。
- **编译等待**: `tc_template/tc_platform.py` — `_poll_build_complete()` 轮询 `BuildState == 2`
- **CLI 入口**: `tc diag health|ping|route|target|errors|license`
- **GUI 入口**: Platform Control → Diagnostics accordion
- **Skill 文件**: `.claude/commands/twincat-diagnostics.md`

## CLI 示例

```bash
# 全系统健康检查
tc diag health

# 测试 ADS 连通性
tc diag ping
tc diag ping 172.16.1.100.1.1

# 路由诊断
tc diag route
tc diag route CX-12345

# 目标详细信息
tc diag target

# 增强错误诊断 (含修复建议)
tc diag errors

# 许可证状态
tc diag license
```

## 关键规则

1. **诊断操作只读** — 不改项目、不激活配置、不切换模式。
2. **离线可用** — XAE 未运行时也能做部分检查（Runtime、Target ping、Routes）。
3. **ADS ping 双方法** — 优先用 pyads，失败后回退到 TCP 48898 端口。
4. **错误诊断是辅助性的** — 模式匹配不保证 100% 准确，复杂问题仍需人工分析。

## 与其他技能的关系

```
/tc-diag (本技能)
    │
    ├─ health ──────→ 全面检查 → 发现问题 → 建议调用 /tc 或 /plc 修复
    ├─ ping/route ──→ 连通性诊断 ─→ /tc target 命令
    ├─ errors ──────→ 错误分析 ──→ /plc build/read/write 修复代码
    ├─ license ─────→ 许可证状态 → 手动处理
    └─ target ──────→ 目标信息 ──→ /tc target/platform 命令
```

## 待扩展

- [ ] **实时监控**: 定期轮询运行状态 (watch)
- [ ] **日志提取**: 从目标控制器拉取 TwinCAT 日志
- [ ] **性能分析**: 读取 PLC 循环时间、CPU 使用率
- [ ] **内存分析**: 检查 Retain/Persistent 变量占用
- [ ] **网络分析**: 扫描子网中所有 Beckhoff 设备拓扑
