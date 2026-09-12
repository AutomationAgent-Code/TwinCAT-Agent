# TwinCAT Library Management

管理 TwinCAT PLC 项目的库引用、占位符、库仓库 —— 全部通过 COM Automation
Interface（PowerShell 桥），**不碰** `.tsproj` 的 `<LibraryReferences>`、也不碰
`_Libraries/` 目录。项目在 XAE 中打开时，所有库操作都走 COM。

## Trigger
```
/plc lib-* | placeholder-* | repo-*   （库相关子命令归在 plc 命令组下）
```

## 前置条件

- TwinCAT XAE（TcXaeShell）正在运行且已打开含 PLC 项目的解决方案
- 底层经 `tc_template/_ps_bridge.py` → `TcCom.ps1` 调 COM，无需 pyautogui

## Commands

### 项目库引用

| 命令 | 用法 | 说明 |
|------|------|------|
| `libraries` | `plc libraries` | 列出当前项目的库引用 |
| `lib-add` | `plc lib-add <name> -v <ver> -d <dist>` | 添加库引用（`-v *` = 最新） |
| `lib-remove` | `plc lib-remove <name> [-v <ver>] [-d <dist>]` | 移除库引用（空 = 任意匹配） |

### 占位符（TwinCAT 的依赖解析机制）

> 占位符声明"我需要某个库"但不锁定具体版本，构建时解析；`placeholder-freeze` 才锁死。

| 命令 | 用法 | 说明 |
|------|------|------|
| `placeholder-add` | `plc placeholder-add <name> --lib <lib> -v <ver> -d <dist>` | 添加占位符（可带默认解析目标） |
| `placeholder-freeze` | `plc placeholder-freeze <name>` | 冻结占位符到当前解析的版本 |

### 库仓库（库的来源位置）

| 命令 | 用法 | 说明 |
|------|------|------|
| `repo-add` | `plc repo-add <name> <path> [-i <index>]` | 添加库仓库（`-i` 指定插入位置） |
| `repo-remove` | `plc repo-remove <name>` | 按名移除库仓库 |

### 系统级安装/扫描

| 命令 | 用法 | 说明 |
|------|------|------|
| `lib-scan` | `plc lib-scan` | 扫描系统上**所有已安装**的库 |
| `lib-install` | `plc lib-install <repo> <path> [--overwrite]` | 从仓库安装 `.compiled-library` |
| `lib-uninstall` | `plc lib-uninstall <repo> <name> [-v <ver>] [-d <dist>]` | 从仓库卸载库 |

## COM API 映射

> 全部走 References 节点（该节点经 IDispatch **同时也是 `ITcPlcLibraryManager`**）。
> `plc.py` 的 `_get_refs_node(sysman)` 取到它，方法名以下表为准（实测于源码）。

| CLI 命令 | 桥函数 (`_ps_bridge`) | References 节点方法 |
|---------|----------------------|---------|
| `libraries` | `com_list_libraries()` | 枚举 References 子节点 |
| `lib-scan` | `com_scan_libraries()` | `ScanLibraries()` |
| `lib-add` | `com_add_library()` | `AddLibrary(name, version, distributor)` |
| `lib-remove` | `com_remove_library()` | `RemoveReference(name, version, distributor)` |
| `placeholder-add` | `com_add_placeholder()` | `AddPlaceholder(name, defaultLib, …)` |
| `placeholder-freeze` | `com_freeze_placeholder()` | `FreezePlaceholder(name)` |
| `repo-add` | `com_insert_repository()` | `InsertRepository(name, rootFolder, index)` |
| `repo-remove` | `com_remove_repository()` | `RemoveRepository(name)` |
| `lib-install` | `com_install_library()` | `InstallLibrary(repo, libPath, overwrite)` |
| `lib-uninstall` | `com_uninstall_library()` | `UninstallLibrary(repo, name, version, distributor)` |

## Typical Workflows

### 给项目加运动控制库
```
plc libraries                                     # 看现有引用
plc lib-scan                                      # 找可用库
plc lib-add "Tc2_MC2" -d "Beckhoff Automation GmbH" -v "*"
plc libraries                                     # 确认已加入
```

### 用占位符声明依赖 + 冻结版本
```
plc placeholder-add "Tc2_Standard" --lib "Tc2_Standard" -d "Beckhoff Automation GmbH"
plc placeholder-freeze "Tc2_Standard"             # 锁到当前解析版本
```

### 添加自定义仓库并安装库
```
plc repo-add "MyRepo" "C:\TwinCAT\MyLibraries"
plc lib-install "MyRepo" "MyCustomLib\1.0.0.0\MyCustomLib.compiled-library"
plc lib-add "MyCustomLib" -d "<distributor>"      # 装完再引用到项目
```

## 关键设计决策

1. **全部走 COM** —— 不手动改 `.tsproj` `<LibraryReferences>` 或 `_Libraries/` 内容
2. **占位符 vs 引用** —— 占位符延迟到构建期解析版本，`freeze` 才锁死；直接引用则立即绑定
3. **仓库是库的来源** —— 加仓库使其中的库可被 `lib-install` 安装
4. **模板里的 `_Libraries` 原样保留** —— 提取/scaffold 时不改动

## 安全规则

- 项目在 XAE 打开时，**不要**边开边手改 `.tsproj` 的 `<LibraryReferences>` 或 `_Libraries/` 目录，否则与 COM 状态冲突
- 卸载/移除是破坏性操作，`lib-uninstall` 从磁盘删除已安装库，谨慎使用

## 参考

- 详细 COM 接口见 `knowledge_base/tc3_automation_interface.md`
- 实现见 `tc_template/plc.py`（COM 实现）+ `tc_template/_ps_bridge.py`（桥包装）+ `tc_template/cli.py`（CLI）
- 相关技能：`/plc`（代码读写）、`/tc`（编译部署）
