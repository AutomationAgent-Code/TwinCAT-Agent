# PLC Programming

TwinCAT PLC code read/write/create — COM (IDE integration, preferred) + filesystem (XML parse/write, offline fallback).

## Trigger
```
/plc <operation> [arguments]
```

## Commands

修改前先读取 [编码规范](plc-coding-standard.md)，写 FB 前先查模板库。此技能只授权用户要求的操作；
创建、修改、回读、编译不隐含登录、启动或部署。全流程边界见 [主技能](twincat-agent.md)。

### Filesystem (no TwinCAT needed)

| Command | Usage | Description |
|---------|-------|-------------|
| `list` | `plc list <dir>` | List all PLC objects by type |
| `read` | `plc read <dir> <name>` | Read declaration + implementation + methods |
| `write` | `plc write <dir> <name> "code" --area impl` | Write to file (CDATA preserved) |
| `export` | `plc export <dir> <name> -o <xml>` | Export PLCopen XML |
| `import` | `plc import <dir> <xml>` | Import PLCopen XML, create file |

### COM (needs TwinCAT running)

| Command | Usage | Description |
|---------|-------|-------------|
| `create-project` | `plc create-project [name]` | Create PLC project (with MAIN) under TIPC |
| `remove-project` | `plc remove-project <name>` | Remove TIPC node; retain local PLC project files |
| `delete-project` | `plc delete-project <name>` | Remove TIPC node AND the local PLC directory precisely referenced by .tsproj |
| `create-com` | `plc create-com <name> -t fb` | Create POU/DUT/GVL via COM |
| `delete-pou` | `plc delete-pou <name>` | Delete POU/DUT/GVL/Interface (DeleteChild) |
| `rename` | `plc rename <old> <new>` | Rename POU/DUT/GVL/Interface |
| `vars` | `plc vars [pou]` | List variables (name : type · scope) |
| `structure` | `plc structure` | PLC project tree (POUs/DUTs/GVLs + methods) |
| `search` | `plc search <pattern> [--regex] [--case]` | Search all POU code |
| `import-com` | `plc import-com <xml>` | PlcOpenImport via COM |
| `export-com` | `plc export-com <out> <pous...>` | PlcOpenExport via COM |
| `build` | `plc build` | Build PLC project + read error list |
| `reload` | `plc reload` | No-op (COM writes auto-reparse) |
| `libraries` | `plc libraries` | List library references in project |
| `lib-scan` | `plc lib-scan` | Scan all installed libraries on system |
| `lib-add` | `plc lib-add <name> -v \* -d "Beckhoff.."` | Add library reference |
| `lib-remove` | `plc lib-remove <name>` | Remove library reference |
| `placeholder-add` | `plc placeholder-add <name> -l Tc2_NC` | Add placeholder reference |
| `placeholder-freeze` | `plc placeholder-freeze <name>` | Freeze placeholder version |
| `repo-add` | `plc repo-add <name> <path>` | Add library repository |
| `repo-remove` | `plc repo-remove <name>` | Remove library repository |
| `lib-install` | `plc lib-install <repo> <path>` | Install library from repo |
| `lib-uninstall` | `plc lib-uninstall <repo> <name>` | Uninstall library from repo |

---

## Library Management (ITcPlcLibraryManager)

All library operations go through `ITcPlcLibraryManager` on the References COM node.
**No CastTo needed** — all methods exposed via IDispatch.

```python
from tc_template.plc import (
    list_libraries, scan_installed_libraries,
    com_add_library, com_remove_library,
    com_add_placeholder, com_freeze_placeholder,
    com_insert_repository, com_remove_repository,
    com_install_library, com_uninstall_library,
)
```

### Add library to project
```python
com_add_library('Tc2_Standard', '*', 'Beckhoff Automation GmbH')
# or by display name:
com_add_library('Tc2_Math, * (Beckhoff Automation GmbH)')
```

### Scan installed
```python
libs = scan_installed_libraries()
# [{name, library_name, version, distributor, display_name}, ...]
```

### Placeholder
```python
com_add_placeholder('Placeholder_NC', 'Tc2_NC', '*', 'Beckhoff Automation GmbH')
com_freeze_placeholder('Placeholder_NC')
```

### Repository
```python
com_insert_repository('MyRepo', 'C:\\MyLibs', 0)
com_install_library('MyRepo', 'C:\\MyLibs\\MyLib.library')
com_uninstall_library('MyRepo', 'MyLib')
com_remove_repository('MyRepo')
```

---

## COM CreateChild Reference

### Navigating the PLC tree

Use the existing Agent/COM bridge bound to the actual XAE PID and solution. Enumerate or
find the requested PLC object once and reuse its exact returned path. Do not attach to
the first registered DTE or assume Solution.Projects.Item(1) is the requested project.
Project deletion must resolve the exact .tsproj reference first; never infer the disk
directory from a display name. Use remove-project only when files should be retained.

### CreateChild parameters
```python
CreateChild(name, subType, bstrBefore, vInfo)
```

### SubType reference
| Type | subType | vInfo (language) |
|------|---------|------------------|
| Program | 602 | IEC language string, e.g. `'ST'` |
| Function Block | **604** | same as above |
| Function | 603 | Use create_pou(pou_type='function', return_type=...) to package the arguments |
| Struct (DUT) | 606 | Initial complete TYPE ... STRUCT ... END_STRUCT ... END_TYPE declaration |
| Enum (DUT) | 605 | Initial complete TYPE ... (...) ... END_TYPE declaration |
| Union (DUT) | 607 | Initial complete TYPE ... UNION ... END_UNION ... END_TYPE declaration |
| GVL | **615** | Initial declaration when supplied; otherwise None |
| Folder | 601 | `None` |
| Interface | 618 | optional extend type |
| Action | 608 | language |
| Method | 609 | Use plc_create_member; do not hand-pack COM arguments |
| Property | 611 | Use plc_create_property to create Property and Get/Set together |
| Visualization | 619 | `None` |

For Program/FB, vInfo is a language string. Other categories have different contracts;
use the high-level wrappers in tc_template/plc.py rather than generalizing this rule.
Create DUTs with the initial declaration: empty creation followed by DeclarationText can
materialize an Alias (623) instead of Struct/Enum/Union on XAE 15. Read back ItemType and declaration.

### Tree path abbreviations
| Abbrev | Meaning |
|--------|---------|
| `TIPC` | PLC projects root |
| `TIRT` | Real-time tasks |
| `TIID` | I/O devices |
| `TINC` | NC (motion) |
| `TIRS` | Real-time settings |

### Nested project tree
```
TIPC
  └── PLC (project instance)
        └── PLC项目 (nested project)
              ├── References
              ├── DUTs
              ├── GVLs
              ├── POUs        ← CreateChild here
              ├── VISUs
              ├── GlobalTextList
              ├── PlcTask
              └── VisualizationManager
```

Use the returned Unicode path unchanged, including nested folders and members; do not reconstruct localized node names.

---

## COM Code Read/Write (ITcPlcDeclaration / ITcPlcImplementation)

**All PLC code read/write now uses COM interfaces — no filesystem, no pyautogui, no dialog.**

### Read code
```python
from tc_template.plc import com_read_pou

info = com_read_pou("MAIN")
# {
#   'name': 'MAIN',
#   'declaration': 'PROGRAM MAIN\nVAR\n...\nEND_VAR',
#   'implementation': 'nCounter := nCounter + 1;',
#   'language': 'ST', 'methods': [...]
# }
```

### Write code
```python
from tc_template.plc import com_write_pou

com_write_pou("MAIN", "nCounter := nCounter + 1;", area="implementation")
com_write_pou("MAIN", "PROGRAM MAIN\nVAR\n\tnCounter : UDINT;\nEND_VAR", area="declaration")
# Write to a Method:
com_write_pou("FB_Motor", "// method code", method_name="Start")
```

### COM interfaces used
| Interface | Property | Direction |
|-----------|----------|-----------|
| `ITcPlcDeclaration` | `DeclarationText` | Read / Write |
| `ITcPlcImplementation` | `ImplementationText` | Read / Write |

Use com_read_pou/com_write_pou and the existing dynamic COM bridge; do not introduce
CastTo/early binding, which conflicts with the Python 3.14 compatibility strategy.

**Key advantage:** COM writes are IDE-internal — TwinCAT re-parses automatically, **no "File Modified" dialog**. No filesystem XML CDATA parsing, no `_tree_to_string()` marker workarounds, no pyautogui Ctrl+S reload needed.

---

## 编译与错误读取

调用现有 `plc build` / `tc build` 或 Agent `plc_build`，不要另写一套窗口选择、清空或固定延时逻辑。
工具综合 XAE 桥接错误列表、Build Output 与构建状态，需检查本次构建完成状态、失败项目数、错误和结果来源。
ErrorItems 为空、接口不可用、超时或旧 LastBuildInfo 都不能单独证明本次“0 错误”。

编译后回读修改对象；编译成功仅证明编译结果，不等于 TE1200 静态分析或在线行为验证。
没有实际执行相应验证就明确标注，未获上线授权不自动登录/启动。

不要调用 `ExecuteCommand("TwinCAT.ClearErrorList")`：某些 Analytics 扩展会命中歧义命令并弹调试对话框。
不要为读错误而 SelectAll、复制剪贴板、抢焦点或全局抑制弹窗。接口差异和回退留给现有桥接层处理。

---

## Removed: PyAutoGUI

PyAutoGUI is **fully removed** from PLC code operations. The `live_edit.py` module has been deleted. Error list reading, code read/write, create POU — all COM. (pyautogui/pyperclip remain installed only for the `tc target add` route-registration GUI.)

---

## PowerShell 替代 Python（无需 pywin32）

同一套 Automation Interface 也可以用 **纯 PowerShell** 驱动，模板见
`tc_template/TcCom.ps1`（`Connect-Tc` / `Read-Pou` / `Write-Pou` / `New-Pou` / `Invoke-TcBuild` /
`Set-TcSilentMode`）。dot-source 载入：`. .\TcCom.ps1`。已端到端实测：读写声明/实现、CreateChild、编译全通。

```powershell
. .\TcCom.ps1
$dte = Connect-Tc
Read-Pou  $dte 'MAIN'
Write-Pou $dte 'MAIN' implementation "nCounter := nCounter + 1;"
New-Pou   $dte 'FB_Test' fb
Invoke-TcBuild $dte          # 返回 FailedProjects(0=成功) + ErrorLines
```

PowerShell 和 Python 都通过现有封装晚绑定读取 DeclarationText / ImplementationText，
不要另加 CastTo 早绑定绕过桥接层。

三个 PowerShell 特有的坑（`TcCom.ps1` 已规避，文件头有注释）：

1. **COM 树项实现 `IEnumVARIANT`（可 `foreach`）**。把它跨 `return` 输出时，PowerShell 管道会
   **展开成子节点** → `.DeclarationText` 变成对 N 个方法/属性的成员枚举（症状：`ItemType` 返回一串值、
   `.Length` 等于子节点数）。对策：**COM 项绝不跨函数边界**，遍历+读写在同一 `foreach` 作用域内用委托
   完成，函数只返回纯数据（`pscustomobject`/`string`）。
2. **PS 5.1 读无 BOM 的 `.ps1` 会误解码中文注释**，导致 `}` 解析错误。脚本须存为 **UTF-8 BOM**。
   中文对象名（如 `xxx项目`）只用**运行时 COM 名**比较，不要在脚本里写中文字面量做匹配。
3. `$PID` 是 PowerShell 保留变量，循环变量改名（如 `$progId`）。

多 XAE 时通过现有 Connect-Tc/桥接层传入明确 PID，不使用 GetActiveObject 的首个实例作为目标。
SilentMode 只在授权的自动化操作范围内临时设置并恢复，不能长期抑制用户手动操作的弹窗。

---

## Filesystem vs COM

| Operation | COM | Filesystem |
|-----------|-----|-----------|
| List objects | ✅ `com_list_objects()` | ✅ |
| Read code | ✅ `com_read_pou()` / PS `Read-Pou` | ✅ XML parse |
| Write code | ✅ `com_write_pou()` / PS `Write-Pou` | ✅ CDATA preserved |
| Create POU | ✅ CreateChild + code write | ❌ |
| Error list | ✅ 现有桥接层综合错误列表、Build Output 和状态，并保留来源 | ❌ |
| Clear error list | ✅ 跳过（**勿用** `TwinCAT.ClearErrorList` — Analytics 弹窗） | ❌ |
| Build wait | ✅ 封装确认本次构建完成与超时，不使用旧状态推断 | N/A |
| No dialog | ✅ IDE-internal | ❌ reload dialog |
| Reliability | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
