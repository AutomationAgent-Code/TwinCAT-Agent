---
name: twincat-agent-workflow-summary
description: 全链路自动化经验索引 — 路由、部署、I/O 链接、代码写入、弹窗关闭、平台模型
metadata:
  type: reference
---

# TwinCAT Agent 全链路自动化 — 经验索引

## 完整工作流

以下是历史全链路实验记录，不是每个请求都要执行的默认计划。当前任务路由以
[主技能](../commands/twincat-agent.md) 为准：只改代码时回读和编译即可，扫描、清理、
路由注册、部署和在线动作必须在用户要求范围内；不要照此记录删除主站或抑制用户弹窗。

```
搜索设备(tc target find) → 路由(tc target add BM_CLICK零鼠标)
→ 创建项目(tc-template create basic) → 切目标+自动检测平台(Release|x64)
→ 扫描(tc scan) → 清空主站 → COM创建PLC+写GVL+MAIN
→ 编译(tc build) → LinkVariables全自动链接 → tc deploy
→ Boot✅ Activate✅ Restart✅ Online(verified:Run)
```

## 关键修复索引

| 修复 | 详情 | 记忆 |
|------|------|------|
| 路由 GUI 零鼠标 | BM_CLICK/WM_SETTEXT 替代 pyautogui | [[route-gui-no-mouse]] |
| deploy 自动 online 验证 | pyads state==5 重试，去掉 CoUninitialize | [[deploy-auto-online-verified]] |
| Boot 正确节点 | `TIPC^PLC1` 根节点调用 GenerateBootProject | [[plc-project-creation-via-com]] |
| I/O 链接路径格式 | GVL 不加 `{attribute 'parameter'}` | [[plc-project-creation-via-com]] |
| Config 模式 & 扫描 | _is_config(7,15), 三级回退, 空主站清理 | [[config-mode-and-scanning]] |
| 弹窗零鼠标 | EnumWindows → BM_CLICK, SilentMode+SuppressUI | [[deploy-auto-online-verified]] |
| 平台自动检测 | TIRS CPUType=86 → x64, 状态文件持久化 | [[build-platform-auto-detection]] |
| IEC 编程易错点 | 注释语法、TON 闪烁、UDINT 溢出、CiA 402 | [[iec-plc-programming-pitfalls]] |
| 库管理 | COM 全生命周期：引用/占位符/仓库/安装 | [[library-management-via-com]] |

## 运行环境
以下是历史实验环境，当前模型、Python/XAE 版本及目标必须实际读取，不能据此推断。
- **模型**: Fast 模式 Claude Opus (4.8)
- **平台**: Win11 + TwinCAT 3.1.4024 + Python 3.12
- **通信**: COM (TcXaeShell.DTE.15.0) + pyads + win32gui (零 pyautogui)

## 已修复的合并冲突
- `_IEC_LANG_MAP` 定义丢失
- `_find_plc_project` 走 NestedProject 属性 vs Child() 遍历

[[route-gui-no-mouse]] [[deploy-auto-online-verified]] [[plc-project-creation-via-com]] [[config-mode-and-scanning]] [[build-platform-auto-detection]] [[iec-plc-programming-pitfalls]] [[library-management-via-com]]
