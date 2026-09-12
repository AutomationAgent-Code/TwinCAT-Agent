# TwinCAT Agent — 汇报 PPT 大纲

## Slide 1: 封面

> **TwinCAT Agent**
> AI 驱动的 TwinCAT 3 自动化开发平台
>
> AI 语音编程 · 模板管理 · 一键部署
>
> 田然 | 2026 年

---

## Slide 2: 痛点与目标

**TwinCAT 开发中的痛点:**

1. 新建项目繁琐 — 每次都从头创建, 没有可复用的模板
2. GUID 噩梦 — 复制项目时 GUID 冲突导致崩溃
3. PLC 编程效率低 — 代码在各种文件之间手动复制
4. 编译部署步骤多 — Build → Activate → Restart → Login → Start 反复手动操作
5. 弹窗需要人工确认 — 无法无人值守部署

**目标:**
> 让 AI 完成重复劳动, 开发者专注核心逻辑

---

## Slide 3: 系统架构

```
Claude Code (AI Agent)
    │
    ├── /twincat-template  模板管理
    ├── /plc               PLC 编程
    └── /tc                平台控制
    │
    ▼
Python Engine (3,763 行, 13 模块, Click CLI)
    │
    ├── 文件系统 (XML CDATA)     ← 代码读写 (最可靠)
    ├── COM (win32com)            ← 创建 POU, 激活配置
    └── pyautogui                 ← 编辑器操作, 弹窗处理
    │
    ▼
TwinCAT 3 XAE (TcXaeShell)
```

---

## Slide 4: 三大 Skill

| Skill | 命令数 | 功能 |
|-------|--------|------|
| `/twincat-template` | 7 | 14 个模板, 创建/提取/验证/删除, GUID 保护 |
| `/plc` | 12 | POU/DUT/GVL 创建读写导入导出, 实时编辑器操作 |
| `/tc` | 15 | 编译/激活/登录/启动/部署, Config/Run 切换 |

**总计: 30+ 命令, 覆盖 TwinCAT 全生命周期**

---

## Slide 5: 模板管理

**GUID 保护策略 — 只替换该换的:**

```
白名单扫描 (仅项目属性)         保留不动 (系统 GUID)
├─ Id="..."                    ├─ {00000000-...} null GUID
├─ ProjectGUID="..."           ├─ {18071995-*} Beckhoff Type
├─ Application/TypeSystem      ├─ {B1E792BE-...} TcXaeShell
├─ GuidA/GuidB/TmcHash         ├─ ProjectExtensions 段
                               └─ Licenses/Device 段
```

**模板库 (从 SPT/Model 项目中提取)**

```
packml ─── HMI + PackML state machine
model1 ── CoE servo axis control
spt-* ─── 12 SPT 示例 (Machine/Camming/Homing/Alarms/MultiMaster...)
```

---

## Slide 6: PLC 编程

**三层工具互补**

| | COM | 文件系统 | pyautogui |
|---|---|---|---|
| 创建 POU | ⭐⭐⭐⭐⭐ | ❌ | ⭐⭐ |
| 读写代码 | ❌ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 编辑器 Live | ❌ | ❌ | ⭐⭐⭐⭐ |

```
最佳组合:
  1. COM CreateChild → 创建空 POU
  2. 文件系统 write_pou → 写入代码
  3. pyautogui Ctrl+S → IDE 刷新
```

**创建 POU 一行代码:**

```python
pous.CreateChild('FB_TrafficLight', 604, '', 'ST')
#                   名称          类型  before  语言
```

**支持类型:** FB (604), Program (603), Struct (606), Enum (605), GVL (615), Visu (619)

---

## Slide 7: 一键部署 (tc deploy)

```
tc deploy  →  6 步全自动

构建  →  激活  →  重启  →  登录  →  启动  →  检查错误
 │        │        │        │        │         │
 │     COM 调用     │     ConsumeXml     │   inspect
 │                  │    (XML 命令)      │
 │            [pyautogui 后台线程消除弹窗]
```

**关键技术:**

1. SilentMode 抑制大部分弹窗
2. 后台线程持续模拟点击 OK
3. 多偏移量覆盖不同弹窗布局
4. Login 失败不中断流程

**结果:** 开发者敲 `tc deploy` → 10 秒后 PLC 在线运行

---

## Slide 8: Live Editing (pyautogui)

```
/plc read <dir> MAIN
  → pyautogui 点击编辑器声明区 → Ctrl+A → Ctrl+C
  → 返回完整代码

/plc write <dir> MAIN "..."  
  → pyperclip 复制代码
  → pyautogui Ctrl+A → Ctrl+V → Ctrl+S
  → 编辑器即时刷新
```

**实现细节:**
- 声明区 (上部 25%) / 实现区 (下部 50%) 分开读写
- CDATA 保护: marker-based 序列化, 不转义 `<![CDATA[...]]>`

---

## Slide 9: 项目统计

| 维度 | 数据 |
|------|------|
| Python 模块 | 13 个, 3,763 行 |
| CLI 命令 | 3 个命令组, 30+ 命令 |
| Skill 文件 | 3 个 |
| 知识库 | 3 个文档, 521 行 |
| 模板 | 14 个 (提取自 14 个源项目) |

**核心模块:**

```
tc_template/cli.py          637 行  ← 最复杂模块
tc_template/extract.py       595 行  ← GUID 保护核心
tc_template/plc.py           556 行  ← 三层工具调度
tc_template/tc_platform.py   468 行  ← 部署流水线
```

---

## Slide 10: 技术突破

1. **GUID 白名单保护** — 从全文件扫描改为属性级别匹配, 彻底解决 Type System GUID 误替换

2. **COM 创建 POU 的方法** — 发现 `vInfo='ST'` 字符串格式 (vs 数组 `['6']`), 避免生成 NWL 梯形图

3. **SilentMode + 线程点击** — 解决 TwinCAT 弹窗在 COM 调用期间阻塞的问题, 实现无人值守部署

4. **CDATA marker 序列化** — 解决 Python XML 库自动转义 CDATA 导致 TwinCAT 无法解析的问题

5. **三层工具分工** — 文件系统 / COM / pyautogui 各司其职, 互补而非替代

---

## Slide 11: 成果演示

**场景 1: 创建项目到部署**

```
$ tc-template create packml -n MyProject -o G:/Prj
  Created: 37 files, 223 GUIDs
  Error List: 16 items (all library missing — expected)

$ tc deploy
  Build: 0 errors
  Online: Login + Start OK
  → 绿灯: PLC 在线运行
```

**场景 2: AI 编程 — 红绿灯 FB**

```
$ tc-template plc create-com FB_TrafficLight -t functionBlock
$ tc-template plc write ... "CASE state OF 0→1→2→0"
$ tc-template plc write ... MAIN "fbTL(bEnable := TRUE);"
$ tc deploy
```

---

## Slide 12: 后续规划

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P0 | 代码对比工具 | git diff .TcPOU 文件 |
| P0 | 模板自动更新 | 项目修改后一键同步回模板 |
| P1 | 库版本管理 | 自动解析 Placeholder → 匹配本地安装版本 |
| P1 | VISU 可视化编程 | HMI 元素通过 COM 创建 + 属性绑定 |
| P2 | 多项目批量构建 | 针对 CI/CD 场景 |
| P2 | 远程目标部署 | SetTargetNetId + 跨网络 deploy |

---

## Slide 13: 总结

**核心理念:** AI 完成重复劳动, 开发者专注核心逻辑

**三大支柱:**
1. 模板管理 — 14 个模板 + GUID 保护 + 自动描述
2. PLC 编程 — COM 创建 + 文件系统读写 + pyautogui 实时编辑
3. 平台控制 — 一键部署 + 弹窗自动处理 + 模式切换

**技术特色:**
- 三层工具互补 (COM / 文件系统 / pyautogui)
- 全生命周期覆盖 (创建 → 编程 → 编译 → 部署)
- 实际验证可用 (与 TcXaeShell 15.0 测试通过)
