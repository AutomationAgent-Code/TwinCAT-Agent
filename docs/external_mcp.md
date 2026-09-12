# 第三方 MCP 接入

TwinCAT Agent 可在“设置 → 第三方 MCP 服务”中接入用户自己的 MCP 服务，例如 PLC 程序模板库、内部代码规范查询或项目文档检索。

支持两种方式：

- `stdio`：在本机直接启动 MCP 程序。适合 Python、Node.js 或独立可执行文件形式的模板服务。
- `Streamable HTTP`：连接现有的 `http` 或 `https` MCP 服务。

## 添加本地模板服务

在设置里选择“本地程序（stdio）”，填写启动命令与 JSON 参数。例如 Python 服务：

```text
启动命令：python
启动参数：["C:\\Tools\\plc-template-mcp\\server.py", "--templates", "D:\\PLC\\Templates"]
工作目录：C:\Tools\plc-template-mcp
```

环境变量可在“高级设置”中填 JSON 对象，例如：

```json
{"TEMPLATE_ROOT":"D:\\PLC\\Templates"}
```

点击“测试连接”后，界面会显示服务发现到的工具数量；保存并启用后，新工具会在下一次对话中提供给 Agent。

## 安全边界

- Agent 用无 Shell 的直接进程方式启动 stdio 服务，不会把命令和参数拼成 Shell 字符串执行。
- 环境变量、HTTP 请求头与其中的密钥仅保存在本机 `tc_agent/config.json`，不会回传到界面或传给模型。
- 外部 MCP 返回给模型的内容会限长，避免模板库意外输出占满对话上下文。
- 每个外部工具均显示为“外部 MCP”，无论当前选择“询问”“编辑放行”还是“自动”，都必须由用户逐次确认；计划模式中不会执行。
- 后台只读工作器不会调用第三方 MCP，防止它在无人确认时访问外部服务。

## 工具命名

Agent 会为发现的工具创建稳定别名：`mcp_<服务ID>_<工具名>`。例如服务 ID 为 `templates`、服务器工具为 `template_find` 时，Agent 使用 `mcp_templates_template_find`。这避免不同 MCP 服务的同名工具相互覆盖。

服务编辑、启用/停用或删除后，当前正在进行的对话会先停止，再更新工具目录，保证模型看到的工具集与实际连接一致。
