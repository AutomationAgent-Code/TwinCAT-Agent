---
name: pyautogui-removed-com-only
description: "PLC code read/write is COM-only; pyautogui live-edit removed (live_edit.py deleted). pyautogui kept only for route GUI"
metadata:
  node_type: memory
  type: project
---

## PLC 代码读写已全部走 COM，pyautogui 写代码方式已移除

2026-06-24 按用户要求删除「pyautogui 写 PLC 代码」的方式：

- **删除** `tc_template/live_edit.py`（整个 pyautogui 编辑器读写模块）。无任何代码 import 它。
- **删除** `tc_template/inspect.py` 的剪贴板回退 `_read_error_list_clipboard` / `_parse_clipboard_lines`（错误列表读取纯 COM）。
- **文档**：`CLAUDE.md`、`.claude/commands/{twincat-template,twincat-helper,plc-programming,twincat-agent}.md`、`README.md` 里所有「pyautogui 写代码 / 点击声明区实现区坐标 / Ctrl+A+V+S」指引改为 COM `com_write_pou`/`com_read_pou`/`create_pou`。

**代码读写唯一正道**：`from tc_template.plc import com_write_pou, com_read_pou, create_pou`
- `com_write_pou(name, code, area="declaration"|"implementation", method_name=None)`
- `com_read_pou(name)` → `{declaration, implementation, methods, ...}`
- `create_pou(pou_type, name, declaration="", implementation="")` — CreateChild + 一次性写代码
COM 写入是 IDE 内部操作，自动重解析，无「文件已被外部修改」弹窗、无需 Ctrl+S、无鼠标。

**pyautogui / pyperclip 仍保留**（pyproject `windows` 可选依赖）：仅供 `tc target add` 的 TcAmsRemoteMgr 路由注册 GUI（`tc_platform.py` 1764+、2172+）。删代码写代码的 pyautogui 时**不要动**这部分，否则破坏 `tc target add`。

**basic 模板是纯空壳**：只有 `.sln`+`.tsproj`，无 PLC 项目、无 MAIN。写代码前先用 COM 建 PLC 项目，见 [[plc-project-creation-via-com]]。COM 调用前需 [[com-dynamic-dispatch-py314]]。

**How to apply:** 写 PLC 代码一律用 `com_write_pou`/`create_pou`，不要再提 pyautogui 点击坐标。改 pyautogui 相关时区分「写代码（已删）」与「路由 GUI（保留）」。
