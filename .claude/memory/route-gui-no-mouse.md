---
name: route-gui-no-mouse
description: 路由添加 GUI 自动化用 BM_CLICK/WM_SETTEXT 替代 pyautogui，窗口前置用 AttachThreadInput
metadata: 
  node_type: memory
  type: project
  originSessionId: d0a449fa-4028-488b-8c80-338f44aa86dd
---

## 路由添加 GUI — 零鼠标方案

TcAmsRemoteMgr.exe 没有 COM 接口，`TcXaeShell.DTE` 也没有路由相关命令（`TwinCAT.AddRoute` 不可用），必须走 GUI。

### 窗口前置 `_bring_to_front`

```python
AttachThreadInput(cur_thread, fg_thread, True)
ShowWindow(hwnd, SW_SHOWNORMAL)
SetForegroundWindow(hwnd)
SetFocus(hwnd)
AttachThreadInput(cur_thread, fg_thread, False)  # detach

# Topmost flick
SetWindowPos(hwnd, HWND_TOPMOST, ...)
SetWindowPos(hwnd, HWND_NOTOPMOST, ...)
```

### 按钮点击 `_btn` — BM_CLICK

```python
PostMessageW(button_hwnd, BM_CLICK, 0, 0)  # 0x00F5
```
不需要窗口在最前，不需要坐标，不需要 pyautogui。

### 文本填入 — WM_SETTEXT

```python
SendMessageW(edit_hwnd, WM_SETTEXT, 0, ip)  # 0x000C
```

### Credential 对话框
- User / Password Edit 从 label 匹配 `_edit_hwnd("user:")`
- Secure ADS 按钮同 BM_CLICK
- Okay 按钮同 BM_CLICK；fallback `PostMessage(WM_KEYDOWN, VK_RETURN)`

### 关键教训
**用 pyautogui.click 即使带了坐标，窗口被遮挡也会点偏。BM_CLICK/WM_SETTEXT 是消息级操作，完全不受窗口遮挡影响。**

[[config-mode-and-scanning]]
