---
name: config-mode-and-scanning
description: "Config mode switching, _is_config() state detection, scan_devices() bugs & fixes, CP panel PC state 15, SilentMode timing"
metadata:
  type: project
---

## Config Mode & EtherCAT Scanning

### ADS State Codes
`0=Invalid 1=Idle 2=Reset 3=Init 4=Start 5=Run 6=Stop 7=Config 8=Reconfig`

EtherCAT hardware scanning via `ProduceXml(False)` requires **Config mode (7)**. Stop(6), Idle(1), Init(3) do NOT work.

### `_is_config()` — Two Fixes

**Bug 1: Wrong state check (最初的 bug)**
```python
# ❌ "not Run" — Stop(6)、Idle(1)、Init(3) 全被当成 Config
return s != 5

# ✅ ONLY Config mode
return s in (7, 15)
```
`read_state()` 返回 Stop(6) 时 `s != 5` 为 True，但 Stop 模式无法扫描。

**Bug 2: CP 面板 PC 上报状态 15**
CP 面板 PC (CP-3BD8F0) 在 `RestartTwinCATConfigMode` 后上报状态 **15**，不是标准 Config(7)。状态 15 时 `ProduceXml(False)` 能正常扫描。面板 PC 的 Runtime 可能合并了额外状态标志位。修复：`return s in (7, 15)`。

症状：扫描输出 "Config mode attempted 3 times — still no devices found"，但单独调用 `ProduceXml` + `RestartTwinCATConfigMode` 能发现设备。

### `set_config_mode()` 三级回退

1. **TIRS ConsumeXml** — `set_config_mode()` 通过 TIRS ConsumeXml 发 Config 命令，但目标可能没真正进入 Config
2. **DTE RestartTwinCATConfigMode** — 强制重启到 Config（等 15s）
3. **ActivateConfiguration + StartRestartTwinCAT** — 兜底方案，pyads 检查 IO 状态还在 Run 时才触发

### `scan_devices()` 流程（已修复）

```
set_silent_mode(True)           # ⚠️ 必须在 set_config_mode 之前
set_config_mode()               # TIRS ConsumeXml
ProduceXml(False) → ScanBoxes
如果 slaves=0 → DTE RestartTwinCATConfigMode + 重试
如果 pyads 显示还在 Run → ActivateConfiguration + StartRestartTwinCAT (兜底)
```

### Bug 3: 空主站清理在 early return 之后

```python
# ❌ 旧代码
if not found_devices:
    return {...}  # ← 直接返回，永远不会清理空主站
# ... 添加设备 ...
for child in list(io_root or []):  # ← 永远执行不到
    ...

# ✅ 修复后
if found_devices:
    # ... 添加设备 ...
# 无论是否发现新设备都执行清理
for child in list(io_root or []):
    if not _has_slave_terminals(child):
        child.DeleteChild(...)
```
场景：第一次 scan 加了 2 个主站但没设备，第二次 scan 没发现新设备 → early return 跳过空主站清理。

### ProduceXml 返回空 ≠ 没有设备

控制器的 `TIRS` 状态为 `ConfigOrRun` 时，`ProduceXml(False)` 可能返回 `<FoundDevices/>` 空，也可能返回设备但 `ScanBoxes` 扫到 0 个从站。同一 PCI 口在未正确 Config 模式下可能报不同 SubType (111=EtherCAT Master vs 112=EAP)。

### SilentMode 必须在 Config 切换前开启

`scan_devices()` 原来没调用 `set_silent_mode(True)`，导致切换 Config 时弹出 "Restart TwinCAT in Config Mode" 对话框。
修复：在 `set_config_mode()` 之前加 `set_silent_mode(True)`。

### Config 模式成功后扫描流程

1. `ProduceXml(False)` → 解析 `<FoundDevices>/<Device>` → 拿到所有兼容 EtherCAT 的物理网口
2. 每个网口：`CreateChild` → `ConsumeXml(ScanBoxes)` → 发现 E-Bus 从站
3. `_has_slave_terminals` 筛选 → 自动 `DeleteChild` 删掉无从站的网口

**How to apply:** `_is_config()` 接受 7 或 15。`set_silent_mode(True)` 在 `set_config_mode()` 之前。空主站清理在扫描末尾无条件执行。三级 Config 回退都走 `_dte()` 同一 COM 实例。

[[deploy-auto-online-verified]] [[route-gui-no-mouse]]
