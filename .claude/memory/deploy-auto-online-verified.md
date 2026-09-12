---
name: deploy-auto-online-verified
description: deploy 全链路：自动 online 带 pyads 验证重试、suppressUI 抑制弹窗、dismiss_dialogs 零鼠标
metadata: 
  node_type: memory
  type: project
  originSessionId: d0a449fa-4028-488b-8c80-338f44aa86dd
---

## deploy 全链路自动 online + 弹窗零鼠标

### `_online_loop` — 替代 `full_online_cycle`

旧的 `full_online_cycle()` 返回 OK 但 PLC 可能没跑。`_online_loop` 每次 online 后用 pyads 验证 state==5 (Run)，不满足则清 DTE 缓存重试（默认 3 次），最后用 `ConsumeXml` 兜底。

```python
for attempt in range(max_retries):
    full_online_cycle()
    plc = pyads.Connection(target, 851, ip)
    state, _ = plc.read_state()
    if state == 5:
        return {"verified": "Run", ...}
    _dte_cache = None  # fresh DTE next iteration
    time.sleep(3 + attempt * 2)

# Fallback: ConsumeXml on Project node
proj.ConsumeXml("<TreeItem>...<LoginCmd>true</LoginCmd><StartCmd>true</StartCmd>...</TreeItem>")
```

### `_dismiss_dialogs` — BM_CLICK 零鼠标

不用 pyautogui。`EnumWindows` 找对话框 → `EnumChildWindows` 找 `BS_DEFPUSHBUTTON` → `PostMessage(BM_CLICK)`。兜底 `PostMessage(WM_KEYDOWN, VK_RETURN)`。

### `set_silent_mode` — 双层抑制

```python
settings.SilentMode = True   # 许可/激活弹窗
dte.SuppressUI = True        # 保存/覆盖弹窗
```

### `deploy()` 关键修复

- 去掉了 `CoUninitialize()` — 重启后销毁 COM 导致 online 拿到坏连接返回假成功
- `_restart_with_retry` 后清 `_dte_cache` 而不是重建 COM
- `set_boot_project` 改用 `TIPC^PLC1` 根节点
- CLI 输出展示 boot/activate/restart 中间结果，`status == "restarted"` 判 OK

**How to apply:** deploy 走 `_online_loop` 不用 `full_online_cycle`。弹窗关闭用 BM_CLICK 不碰 pyautogui。SilentMode+SuppressUI 双层挡弹窗。不要 `CoUninitialize`。

[[config-mode-and-scanning]] [[route-gui-no-mouse]] [[plc-project-creation-via-com]]
