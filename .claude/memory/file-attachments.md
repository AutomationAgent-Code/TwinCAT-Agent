---
name: file-attachments
description: 面板"文件输入"(EPLAN 图纸等)—— 文字提取(任何模型)+ 视觉块(多模态)双链路设计
metadata:
  type: reference
---

# 面板文件输入(EPLAN PDF/Excel/图片 等)

用户要"喂图纸"。EPLAN 原生 .elk/.zw1 私有二进制不解析;实用做法 = 从 EPLAN 导出
PDF 或 Excel/CSV。核心矛盾:图纸是视觉的,但用户默认模型 DeepSeek 是纯文本、看不了图。
所以做成**两条链路**:

1. **文字提取(任何模型可用,含 DeepSeek)** —— `tc_agent/attachments.py`:
   PDF→pypdf、xlsx→openpyxl、csv/文本→直接解码。抽出的文字**并入 user 消息文本**
   (持久进上下文),故文本模型也能读器件位号/端子/网络标号/I-O 清单。
   实测:DeepSeek 正确读出 CSV 里 3 个点位地址(%IX0.0/%QX0.1/%IW2)。
2. **视觉块(仅多模态模型)** —— 图片/PDF 作为图像发给模型。**门控**:每个 Provider
   加 `vision` 布尔开关(像 proxy),勾了才发视觉块;纯文本模型不勾(否则 image_url 会报错)。
   视觉块**只在本轮首个模型调用随图发出、不入持久历史**(base64 巨大,会撑爆滑动窗口预算)。

## 关键实现点
- `attachments.process(atts, vision)` → `{text, visual:[{kind:image|pdf,media_type,data_b64,name}], meta}`。
  每文件文字上限 20k、总 60k;视觉单文件 ≤8MB;PDF 取前 40 页、xlsx 前 800 行。
  解码兜底 utf-8→gb18030→latin-1(国内 EPLAN/Excel 常 GBK)。
- 视觉块按协议翻译(`agent_core._openai_attach_visual`/`_anthropic_attach_visual`):
  OpenAI 兼容 = `image_url` data URL(**PDF 跳过**,chat 无通用图文块,靠抽的文字);
  Anthropic = `image` 块 + **PDF 用 `document` 块**(Claude 原生读图)。
  追加到**最后一条 user 消息**——整轮里 tool 结果是 role=tool、不是 user,所以"最后的 user"
  始终是本轮问题,跨轮不会误挂到旧消息;老轮视觉块不持久=不会重发。
- provider 方法签名多了 `extra_user_content=`;backend `stream_complete/call_model` 多了 `visual=`,
  `run_turn` 只在 `step==0` 传 `turn_visual`。`turn_visual` 是 handler 作用域变量,每个 user 轮重置。
- backend `type:user` 现同时收 `attachments`;`attach.process` 放 `asyncio.to_thread`(PDF/xlsx 解析阻塞);
  user 事件带 `attachments`(元信息)供 UI 回放 chip;user 消息存的是"原文+抽取文字"。
- UI:footer 改成列(chip 条 + 输入行),📎 按钮/拖拽到 footer/粘贴图片;15MB/文件上限;
  已选 chip 可移除;user 气泡内渲染附件 chip;Provider 表单加"支持读图/PDF(多模态)"复选框。

## 依赖 / 打包
- 新增运行时依赖 **pypdf + openpyxl(+et_xmlfile)**,全纯 Python → 便携包能内置。
  `build_portable.ps1` 已把 websockets/pypdf/openpyxl/et_xmlfile 一起拷进 runtime site-packages。
  `pyproject.toml` 加了 `[agent]` extra(websockets/pypdf/openpyxl)。
- **升级已装机器要连后端一起换**:光换 webview/index.html 不够(旧后端不认 attachments、缺 pypdf)。
  完整做法 = 重发便携 zip 重装;或做 app+site-packages 增量包。

相关:[[coagent-backend-operations]]、[[portable-packaging]]、[[te2000-mapped-symbols]]。
