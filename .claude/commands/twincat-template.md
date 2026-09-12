# TwinCAT Template Manager

TwinCAT PLC 项目模板管理 — 创建、提取、检查、浏览。

## 触发

```
/twincat-template <operation> [arguments]
```

## 命令一览

| 命令 | 用法 | 说明 |
|------|------|------|
| `list` | `tc-template list` | 列出所有模板（含自动描述） |
| `info` | `tc-template info <name>` | 模板元数据、变量、GUID 策略 |
| `create` | `tc-template create <tpl> -n <proj> -o <dir>` | 创建→自动固定版本→打开→检查错误 |
| `add` | `tc-template add <path> -n <name>` | 从项目提取模板（含自动描述） |
| `inspect` | `tc-template inspect` | 读取已打开 TwinCAT 的错误列表 |
| `validate` | `tc-template validate <name>` | 验证模板完整性 |
| `remove` | `tc-template remove <name>` | 删除模板 |

---

## create — 一步到位

```
tc-template create packml -n MyProject -o G:/Prj
```

```
1. 读取当前 target NetId + TwinCAT 版本
2. scaffold → 替换 {{PROJECT_NAME}} / {{PLC_NAME}} / {{GUID_N}}
3. Patch .tsproj → TcVersion + TcVersionFixed + TargetNetId
4. os.startfile(.sln) → XAE 自动以固定版本打开
5. 等待加载 → 读取 Error List
```

**版本固定机制（关键）：**
创建后在 `.tsproj` 写入两个属性，XAE 打开时自动使用对应版本：

```xml
<TcSmProject TcVersion="3.1.4024.71" TcVersionFixed="true" ...>
  <Project ProjectGUID="..." TargetNetId="169.254.70.123.1.1" Target64Bit="true" .../>
</TcSmProject>
```

- `TcVersion="3.1.4024.71"` — 指定目标版本
- `TcVersionFixed="true"` — **必须**，XAE 靠此属性判断是否锁定版本
- `TargetNetId` — 绑定目标设备

**版本策略（优先级）：**
1. 从已打开项目的 `TcVersion` 直接读取 → 最准确
2. 从缓存读取（deploy 时自动缓存）→ 离线可用
3. 无 XAE 也无缓存 → 本地安装版本（会提示可能不匹配）
4. `--tc-version 3.1.4024.71` 手动指定 → 最高优先级

**示例输出：**
```
  Target: 169.254.70.123.1.1
  TwinCAT: 3.1.4024.71  (from cache)

  Template:   basic
  Project:    TestV3
  TcVersion:  3.1.4024.71
  TcVersion: 3.1.4024.71 (fixed), Target: 169.254.70.123.1.1
  Opening: G:/prg/TestV3/TestV3.sln
  Error List: 0 items (clean)
```

创建完成后，用 **COM** 写 PLC 程序（IDE 内部操作，自动重解析，无弹窗、无鼠标、无 Ctrl+S）：

### 写 MAIN 声明区 / 实现区
```python
from tc_template.plc import com_write_pou, com_read_pou

decl = '''PROGRAM MAIN
VAR
    (* your variables here *)
END_VAR'''
impl = '''(* your implementation code *)'''

com_write_pou("MAIN", decl, area="declaration")
com_write_pou("MAIN", impl, area="implementation")

info = com_read_pou("MAIN")   # 读回校验
```

### 创建 POU/DUT/GVL (COM)
```
plc create-com <name> -t fb     → Function Block
plc create-com <name> -t struct → Struct DUT
plc create-com <name> -t enum   → Enum DUT
plc create-com <name> -t gvl    → Global Variable List
plc create-com <name> -t program → Program
```

### 创建对象并一次性写代码（CreateChild + 写声明/实现）
```python
from tc_template.plc import create_pou
create_pou("gvl", "GVL_IoLink",
           declaration="{attribute 'qualified_only'}\nVAR_GLOBAL\n  (* ... *)\nEND_VAR")
create_pou("fb", "FB_Motor",
           declaration="FUNCTION_BLOCK FB_Motor\nVAR_INPUT\nEND_VAR",
           implementation="// body")
```

### 空壳项目（basic）先加 PLC 项目
`basic` 模板只生成 TwinCAT 外壳，无 PLC 项目/无 MAIN。写代码前先用 COM 建 PLC 项目（自带 MAIN）：
```python
plc = sysman.LookupTreeItem("TIPC")
plc.CreateChild("PLC1", 0, "", "Standard PLC Template")   # 含 MAIN PRG
com_write_pou("MAIN", decl, area="declaration")
```

### 完整工作流示例
```
tc-template create model1 -n TestProj -o G:/Prj    → 创建项目 + 打开
plc create-com GVL_IoLink -t gvl                     → COM 创建 GVL
com_write_pou("GVL_IoLink", ..., area="declaration") → COM 写 GVL 变量声明
com_write_pou("MAIN", ..., area="declaration"/"implementation") → COM 写 MAIN
tc build                                              → 编译检查
tc deploy                                             → 部署到 target
```

---

## add — 提取模板（含自动描述）

```
tc-template add reference/MyProject -n my-plc
tc-template add G:/Prg/PackML_Test -n packml
```

```
1. analyze_project() → 检测项目名、PLC名、文件数、对象数
2. GUID 白名单属性扫描（Id/ProjectGUID/Application/TypeSystem 等）
   保护: {00000000-...} null GUID
        {18071995-*} Beckhoff 类型系统
        {B1E792BE-...} TcXaeShell
        {08500001-...} TcPlc30 CLSID
        <ProjectExtensions>/<Licenses>/<Device>/<Placeholder*> 段
3. 项目 GUID → {{GUID_1}}...{{GUID_N}}
4. 项目名/PLC名 → {{PROJECT_NAME}} / {{PLC_NAME}}
5. _Libraries 原样复制，_Boot/_CompileInfo/.vs 跳过
6. _generate_description() → 分析 MAIN POU 代码生成描述
7. 输出 template.yaml + _GUID_map.json
```

### 自动描述示例

```
packml:  HMI + Production Monitor support | SPT Machine state machine | 10 POUs, 11 DUTs, 3 VISUs | 20 libs
spt-camming: SPT Machine state machine | NC Motion Control | 4 POUs, 8 DUTs, 1 VISUs | 7 libs
model1: CoE servo axis control | 5 POUs, 9 DUTs, 1 GVLs, 2 VISUs
```

---

## 当前模板库（14 个）

```
packml, model1,
spt-simplemachine, spt-axishoming, spt-camming, spt-alarms,
spt-modemanager, spt-controlledstop, spt-externalsequence,
spt-multimaster, spt-mutingalarms, spt-vm-axis,
spt-ax5000-soe-reset, spt-dynamic-axis
```

---

## 技术要点

- **COM**: win32com `GetActiveObject("TcXaeShell.DTE.15.0")` 或 `Dispatch()` 启动新实例
- **Error List**: TcXaeShell 无 DTE2 ToolWindows 接口，用 ExecuteCommand + Ctrl+A/C 复制文本
- **GUID**: placeholder 策略，白名单属性扫描，大小写不敏感替换
- **_Libraries**: 提取和创建都原样保留，不做任何处理
