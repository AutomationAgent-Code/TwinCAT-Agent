---
name: plc-project-creation-via-com
description: "Create PLC project, POU, GVL via COM — correct SubType, vInfo format, and path resolution"
metadata: 
  node_type: memory
  type: project
  originSessionId: 90fba888-d289-4c9a-90d4-5a69e40b105f
---

## PLC 项目与 POU 的 COM 创建要点

### 创建 PLC 项目

在空 `.tsproj` (basic 模板) 中没有 PLC 项目时，直接用 COM 在 TIPC 上创建：

```python
tipc = sysman.LookupTreeItem("TIPC")
proj = tipc.CreateChild("PLC1", 0, "", "Standard PLC Template")
#                                            ^^^^^^^^^^^^^^^^^^^^^^^^
#                                            vInfo = 模板名（知识库 7.1 节）
```

注意：不能用 `tc-template create` 换模板 — 用户创建项目后只允许在原项目内操作。

### POU vInfo 必须是 2 元素数组

原始代码 `_POU_TYPES_COM` 里 vInfo 只填了 1 个元素 `[str(lang)]`，TwinCAT 报错 `"vInfo contains less than 2 Elements"`。修复为 `[lang, lang]`。

```python
# ❌ 旧
v_info = [str(_IEC_LANG_MAP.get(language, 6))]
# ✅ 新
v_info = [lang, lang]
```

### IEC 路径查找

- PLC 项目树: `TIPC^PLC1^PLC1 Project^POUs^MAIN`
- GVL 变量: `TIPC^PLC1^PLC1 Instance^<VarGrp>^GVL_IoLink.<var>`
- I/O 变量: `TIID^<Device>^<Term>^<Channel>^<Entry>`

### 代码写入 — 优先级

1. **COM `DeclarationText` / `ImplementationText`** — 直接写属性，无弹窗，最快
2. ~~文件系统 CDATA~~ — 会弹"外部修改"提示
3. ~~pyautogui~~ — 受窗口遮挡影响

```python
pou = sysman.LookupTreeItem('TIPC^PLC1^PLC1 Project^POUs^MAIN')
pou.DeclarationText = "PROGRAM MAIN\nVAR\n...\nEND_VAR"
pou.ImplementationText = "// code"
```

### GenerateBootProject — 正确节点

**必须在 `TIPC^PLC1` 根节点调用**，`NestedProject` (`PLC1 Project`) 和 `Instance` (`PLC1 Instance`) 无此方法：

```python
plc_root = tipc.Child(1)  # TIPC^PLC1
plc_root.GenerateBootProject(True)
```

### I/O 链接 — 路径格式

去 `{attribute 'parameter'}` 后变量在 Instance XML 中为 `GVL_IoLink.xxx`：

```python
plc_path = 'TIPC^PLC1^PLC1 Instance^PlcTask Inputs^GVL_IoLink.bDI_00'
io_path  = 'TIID^Device^Term 1 (EK1100)^Term 3 (EL1008)^Channel 1^Input'
sysman.LinkVariables(plc_path, io_path)
```

### 注意

- `{attribute 'Tc2GvlVarNames'}` 会让变量变成 `.xxx` 短名；去掉后变成 `GVL_IoLink.xxx` 全名
- `_find_plc_project` 应走 `NestedProject` 属性，不是遍历 `Child()`
- `_IEC_LANG_MAP` 合并时容易丢失，`create_pou` 里 vInfo 只传语言字符串不用数组

**How to apply:** 新建 PLC 项目用 `TIPC.CreateChild("name", 0, "", "Standard PLC Template")`；POU vInfo 填 2 元素数组；instance 变量路径在 build 后才 populate。Boot 在 TIPC 子节点调。写代码优先 COM 属性。
