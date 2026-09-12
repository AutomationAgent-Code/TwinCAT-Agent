# TE2000 `ITcHmiProject` 工具覆盖

`tc_hmi_project_api` 使用当前机器安装的 `TcHmiAutomation.dll`，通过
`ITcHmiProject` 调用官方接口，不直接改写 `tchmiconfig.json` 或页面 XML。

先读取目录：

```powershell
py -3.14 -m tc_template.cli hmi project-api catalog --project HmiDashboard
```

读取操作直接执行；变更、构建、浏览器预览和发布默认只返回预览，明确提供
`--apply` 后才执行。`arguments` 是 JSON 对象，也可以传 JSON 文件路径。

## 覆盖范围

- 工程配置：`GetConfigValue`、`ChangeConfig`、Locale、LoginPage、ScaleMode、StartupView、Theme、WebSocket 参数。
- 工程项：`AddView`、`AddContent`、`AddUserControl`、`AddTheme`。
- 工程动作：`Build`、`Clean`、`RefreshSymbols`、`ToggleSubscriptionMode`、`Rename`、`ShowInBrowser`。
- 包与发布：NuGet source/package、Profile、Publish 状态、`Publish`。
- 符号与本地化：InternalSymbol、LocalizationEntry、Symbol/Mapping 实例。
- 官方对象实例：Server、Permissions、Recipes、Recording/Historize、File/Control/Symbol 权限对象。

`ITcHmiProject.GetControlInstance` 在当前 TE2000 1.12 实现中要求内部编辑器节点
上下文。工具会调用并验证 `ITcHmiFile.GetControl`，若外部节点无法被 TE2000
接受则返回 `status=unavailable` 和明确说明，不把已知限制伪装成成功或抛出裸 COM 错误。

页面、控件、事件、绑定、主题、本地化和 HMI Server 等高层流程仍优先使用各自的
专用工具；它们提供更严格的 Schema、备份、引用门禁和回读。
