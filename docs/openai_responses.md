# OpenAI 官方 API（v1.0.8.107）

设置 → 添加 Provider → 快速模板「OpenAI 官方（Codex / Responses）」。

| 字段 | 填写 |
| --- | --- |
| Base URL | `https://api.openai.com/v1` |
| 协议 | `responses`（界面显示 OpenAI Responses） |
| API Key | 用户自己的 OpenAI Platform API Key；不要发到聊天或报告里 |
| 模型 | 模板示例 `gpt-5.3-codex`，可填写账号实际获准的其他模型 ID |
| 思考 | 默认由模型决定；关闭对应 low，开启对应 high，不强制发送 none |
| 代理 | 留空直连，不继承系统代理；需要代理时填写实际 HTTP 代理地址 |

API 单独计费，ChatGPT 订阅登录不能替代 Platform API Key。模型示例不是可用性承诺。
自动协议按精确主机名识别 `api.openai.com`；第三方兼容地址仍默认 Chat Completions，
用户可显式选择协议。切换模板不改已保存 Key，避免泄漏密钥到 UI。

## 协议与安全边界

- POST `/responses`，支持完整 JSON 与 SSE，文本、图片、PDF、function calling。
- `store=false`，不依赖服务端 `previous_response_id`，项目 SQLite 是历史权威来源。
  此设置不等于承诺供应商零数据保留，仍适用账号的数据政策。
- 完整 output（含加密 reasoning、message、function_call）作为 `responses_state` 随
  assistant 消息保存；同 endpoint/model/key 才重放，切换 Provider 使用中立历史。
- 参数/结果以 `call_id` 配对，不用 output item 的 `id`。恢复时给未完成工具补中断结果。
- 仅 `response.completed` 且全部参数合法才返回工具调用；不完整、失败、EOF、重复 ID、
  非对象参数一律报错，不能用 `{}` 代替坏参数继续执行。
- 工具保持现有可选参数契约（`strict=false`），仍经过同一个审批、写前门禁、账本。
  `parallel_tool_calls=false`；不启用 OpenAI 托管 shell 或绕过本地权限的工具。
- 加密状态不作为思考文字展示，不送入摘要正文。上下文压缩仍按用户轮次边界。
- 停止后不接受迟到的输出；辅助 HTTP 线程在下一个事件关闭流，完全无数据时仍受
  原有 socket 超时约束，不承诺立即终止供应商正在计算的请求。

## 打包与验证

主 Agent 与 32 位辅助代码分别编译。必要 PowerShell 兼容资源以生成的字节码模块
打入两份工具包，运行时在进程专用临时目录展开；分发物不携带散装 `.py/.ps1` 源码。
这是混淆而不是不可逆保密，不能在资源或字节码中放密钥。

协议回归使用虚构 Key、本地 HTTP 服务与临时 SQLite；不调用收费 API 或真实 PLC 写工具。
实际官方账号连通性、配额、代理及费用需要用户填 Key 后验收。

官方依据：

- https://developers.openai.com/api/docs/guides/migrate-to-responses
- https://developers.openai.com/api/docs/guides/function-calling
- https://developers.openai.com/api/docs/guides/streaming-responses
- https://developers.openai.com/api/docs/models/gpt-5.3-codex
