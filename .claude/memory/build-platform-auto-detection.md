---
name: build-platform-auto-detection
description: "Build platform detection via target CPUType, state file persistence, COM Activate method"
metadata: 
  node_type: memory
  type: project
  originSessionId: 90fba888-d289-4c9a-90d4-5a69e40b105f
---

## 构建平台自动检测与切换

> 2026-09-08 更正：下述 CPUType 映射与默认 x64 已废弃，不得执行。
> 目标平台应读 Beckhoff SystemService GetDetailedDeviceInfoCommand 的 Platform；
> 本机已实测成功。不可用时返回未知，不猜平台。当前平台用 DTE 实时读取，
> 不用旧状态文件冒充。PLC/HMI build 均需先通过对应平台门禁。
> 详见 docs/runtime_transition_contract.md 的最新平台章节。

### 1. 检测依据：TIRS `<CPUType>` 而非控制器名称

以前靠路由表里的设备名称（CP-xxx / CX90xx）猜架构，不可靠。正确做法是读目标控制器 `TIRS` 的 `<TargetCPUInfo><CPUType>`：

- `CPUType=86` → Intel x64 → `TwinCAT RT (x64)`
- 其他非 86 值 → ARM → `TwinCAT OS (ARMV7-A)`
- 读不到 TIRS → 兜底 `TwinCAT RT (x64)`

```python
import xml.etree.ElementTree as ET
tirs = sysman.LookupTreeItem("TIRS")
xml = tirs.ProduceXml(False)
cpu_type = int(ET.fromstring(xml).find(".//CPUType").text)
```

### 2. COM 不能区分子平台

`SolutionBuild.ActiveConfiguration.Name` 只返回 `"Debug"` 或 `"Release"`，不带平台后缀（如 `TwinCAT RT (x64)` vs `TwinCAT RT (x86)`）。CLI 的 `tc platform show/list` 通过解析 `.sln` 文件的 `GlobalSection(SolutionConfigurationPlatforms)` 获取完整列表。

### 3. 平台状态文件

因为 COM 无法精确定位当前子平台，切换后写入 `{sln_dir}/.claude/_build_platform.json`，后续读取优先使用这个文件。

### 4. 切换用 `.Activate()` 方法

`SolutionConfigurations.Item(index).Activate()` 是 Visual Studio DTE 的标准方式。`ActiveConfiguration = cfg` 赋值在某些 TwinCAT 版本上不支持。

### 5. `tc deploy` 在 build 前自动调用 `set_build_platform()`

确保每次部署都用正确的目标架构平台。`build_plc_project()` 也改为从 `get_build_platform()` 读取平台名，不再硬编码 `"Release|TwinCAT RT (x64)"`。

**How to apply:** 平台检测始终读 TIRS CPUType；切换后写状态文件；deploy/build 流程内置自动平台对齐。
