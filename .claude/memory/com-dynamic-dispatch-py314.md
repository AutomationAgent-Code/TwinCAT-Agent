---
name: com-dynamic-dispatch-py314
description: "Python 3.14 + pywin32 early-binding crashes TwinCAT COM (LookupTreeItem access violation); force dynamic dispatch"
metadata:
  node_type: memory
  type: project
---

## Python 3.14 下 TwinCAT COM 必须强制动态绑定

本机解释器是 **Python 3.14**（`C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python314`）。在 3.14 上，pywin32 的 **gen_py 早绑定**包装器（如 `ITcSysManager17`）调用 TwinCAT Automation Interface 方法（`LookupTreeItem`、`.DeclarationText=` 等）会**硬崩溃**：进程退出码 `-1073741819` = `0xC0000005` 访问冲突，无 Python traceback。

**触发条件**：`dte.Solution.Projects.Item(1).Object` 被 pywin32 自动包成 gen_py 早绑定对象（gen_py 缓存里存在 TwinCAT typelib），随后任意方法调用即崩。`win32com.client.dynamic.Dispatch(obj._oleobj_)` 包一层**也救不了**，只要缓存里有早绑定模块就崩。`gencache.EnsureDispatch` 会重新生成早绑定并立刻崩 + 再次污染缓存。

**根因修复**（已落地 `tc_template/_com.py` 的 `force_dynamic_dispatch()`，由 `plc._com_dte` 和 `tc_platform._dte` 在取 DTE 前调用）：

```python
import win32com
from win32com.client import gencache
gencache.is_readonly = True                       # 禁止再生成早绑定
shutil.rmtree(win32com.__gen_path__, ignore_errors=True)  # 清掉已有早绑定模块
```

清缓存 + is_readonly 后，`.Object` 返回纯动态 `win32com.client.CDispatch`，`LookupTreeItem`/`DeclarationText` 全部正常。

**动态绑定下仍可用的写法**：`for child in tree_item` 枚举正常（DISPID_NEWENUM），`item.ChildCount` / `item.Child(i)`（1-based）正常。不需要把代码里的 `for child in plc` 循环改成索引式。

**已知动态绑定下失效**：`dte.ToolWindows.ErrorList.ErrorItems` 读错误列表会抛 `...DTE.15.0.ToolWindows`（属性解析失败）。改用 `dte.Solution.SolutionBuild.LastBuildInfo`（0 = 全部成功）+ `BuildState`（3 = Done）判断编译结果，比 ToolWindows 可靠。

**DTE ProgID 版本**：TwinCAT 3.1 **4026** 的 TcXaeShell 在 ROT 里注册为 **`TcXaeShell.DTE.17.0`**（不是 15.0）。`_com_dte`/`_dte`/`_get_dte` 的 ProgID 尝试列表必须包含 `TcXaeShell.DTE.17.0`，否则 `GetActiveObject` 连不上（报"操作无法使用"）。列表现为 `17.0 → 15.0 → 14.0 → VisualStudio.DTE.17.0`。排查时用 `pythoncom.GetRunningObjectTable().EnumRunning()` 看真实 moniker（含 `!TcXaeShell.DTE.17.0:<pid>`）。

**How to apply:** 任何走 win32com 调 TwinCAT COM 的脚本，先调 `force_dynamic_dispatch()`（或手动清 gen_py + `is_readonly=True`）。连接时 ProgID 列表要含 `TcXaeShell.DTE.17.0`（4026）。判定编译成功用 `LastBuildInfo==0`，别依赖 ToolWindows。用 Windows 原生 Python 跑，别用 Bash 工具的 MSYS python（会段错误）。相关：[[plc-project-creation-via-com]]、[[pyautogui-removed-com-only]]。
