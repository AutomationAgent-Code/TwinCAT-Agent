# TwinCAT HMI Engineering 工程与自动化契约

本文记录 TwinCAT Agent 对 TE2000 TwinCAT HMI 工程的可验证理解，作为工具实现和后续
生成规则的基线。具体项目以其 `.hmiproj`、已安装 Framework 和 Beckhoff 官方文档为准，
不得用新版本经验覆盖旧工程。

## 官方资料入口

- Framework 总览：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/6822765067.html>
- Framework Control 介绍：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/4874286091.html>
- Framework Control 基本结构：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/8973131531.html>
- Framework Packages：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/1780948468319168378379.html>
- 系统要求与版本信息：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/2669710091.html>
- 发布到 HMI Server：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/2669768203.html>
- ADS Runtime 配置：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/9276077579.html>
- Automap Symbols：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/17249397003.html>
- Localizations：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/2669756683.html>
- Localization Editor：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/2669763595.html>
- Themes：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/49339384437888684299.html>
- Theme 工程结构：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/4939601419.html>

## 工程模型

TwinCAT HMI 是 XAE 解决方案中的独立项目类型，不是 System Manager 的树节点。主要对象为：

- `.hmiproj`：MSBuild 工程清单；声明 Target Framework、HMI/Engineering 版本、启动页面、
  主题、输出目录、Server 地址和所有参与构建的 `Content`。
- `.view`：完整页面，根控件通常为 `TcHmiView`。
- `.content`：可复用内容区域，根控件通常为 `TcHmiContent`。
- `.js/.ts/.css`：客户端行为和样式。
- `Themes/`、`Localization/`、`Images/`、`Fonts/`：主题、本地化与静态资源。
- `Server/<extension>/`：Server Extension 配置和 Storage 文件；ADS 配置通常位于
  `Server/ADS/ADS.Config.default.json` 与 `ADS.Config.remote.json`。
- `bin/Default.html`：成功构建后生成的入口页之一；它只能证明构建产物存在，不代表已经发布。

当前实测的最初模板工程为 `native1.12-tchmi`，Engineering 版本为 `14.3.379.1`。
工具必须读取项目字段后再生成内容，不得默认项目已升级到 1.14。

## Markup 和控件

`.view/.content` 是 HTML 风格的 Markup。控件通过 `data-tchmi-type` 指定完整类型，例如：

```html
<div id="StartButton"
     data-tchmi-type="TcHmi.Controls.Beckhoff.TcHmiButton"
     data-tchmi-text="Start"
     data-tchmi-left="20"
     data-tchmi-top="20"
     data-tchmi-width="120"
     data-tchmi-height="40">
</div>
```

自动生成与校验至少遵守以下规则：

1. 页面内每个控件必须有非空、唯一的 `id`。
2. 每个控件必须有完整 `data-tchmi-type`；不能猜测未安装的控件包。
3. 原样保留 `data-tchmi-*` 表达式和绑定语法，不把绑定当普通常量改写。
4. 生成前检查目标 Framework；版本专属属性只能在对应项目上使用。
5. 文件加入磁盘后还必须加入 `.hmiproj` 的 `<Content Include>`，否则设计器或构建可能看不到。

Framework Control 由 HTML、JavaScript/TypeScript、CSS 和 `Description.json` 等文件组成。
它是独立包级开发能力，不应与普通页面控件拼装混为一个写入接口；后续应提供专门的脚手架、
包描述校验和版本兼容检查。

## ADS Runtime 与符号

ADS Server Extension 的 Runtime 配置包含名称、AMS NetId、ADS Port 和启用状态。一个 HMI 工程
可以配置多个 Runtime，PLC 端口不能固定为 851，也不能根据项目顺序猜测。当前第一阶段工具仅
解析已保存的 default/remote JSON，不连接 PLC、不声称符号存在，也不自动执行 Automap。

后续写入 ADS 配置时必须：

1. 默认只预览差异。
2. 校验 NetId 格式、端口范围和 Runtime 名称唯一性。
3. 保存旧文件并在写入后重新解析回读。
4. 把“配置存在”“ADS 可达”“符号可解析”作为三个独立状态报告。
5. 不把开发机默认 `127.0.0.1.1.1:851` 带入交付项目。

## 构建、运行与发布

构建通过 HMI 工程自己的 MSBuild/DTE 目标生成 `bin` 内容。Agent 使用
`SolutionBuild.BuildProject` 并读取 `LastBuildInfo`，不依赖 Error List 的焦点或剪贴板。
发布是另一项外部状态变更：会把工程传输到 HMI Server，常见服务端口为 HTTP 1010、HTTPS
1020。没有显式发布工具调用和目标回读时，只能报告“构建成功”，不能报告“部署成功”。

未来发布工具必须要求显式目标、协议和人工确认；写后验证目标项目版本/入口页，不在普通 build
命令中暗含发布、服务重启或 PLC Runtime 操作。

## TwinCAT Agent 第一阶段工具边界

| 工具 | 状态 | 边界 |
|---|---|---|
| `tc_hmi_project_info` | 只读 | 工程路径、版本、启动页、主题 |
| `tc_hmi_structure` | 只读 | 文件清单与分类 |
| `tc_hmi_read` | 只读 | 文本、控件、属性和绑定 |
| `tc_hmi_ads_info` | 只读 | 已保存 Runtime 配置，不连接 PLC |
| `tc_hmi_validate` | 只读 | 静态结构校验，不代替浏览器运行测试 |
| `tc_hmi_create_view` | 项目写入 | 默认预览；创建、工程登记、XAE 重载、回读或回滚 |
| `tc_hmi_control_edit` | 项目写入 | 控件 add/update/remove；限制属性命名，XAE 重载后按 ID 回读 |
| `tc_hmi_control_events` | 项目写入 | 默认以 `.onName` 写入下方原生事件组；仅明确特殊需求使用 `placement=custom` 写入上方 Custom |
| `tc_hmi_delete_view` | 项目写入 | 拒绝删除启动页；备份后删除文件、工程登记和 Framework 条目 |
| `tc_hmi_ads_runtime_set` | 项目写入 | 校验 NetId/端口，保留 SYMBOLS，更新 default/remote 后回读 |
| `tc_hmi_ads_symbols` | 只读 | 列出保存的 INDEXGROUP、INDEXOFFSET、TYPENAME 映射 |
| `tc_hmi_ads_symbol_set` | 项目写入 | 按已安装 TcHmiAds schema 预览/增改/删除映射并回读 |
| `tc_hmi_bind_plc` | 项目写入 | 从 XAE 读取真实 Target/PLC ADS 端口，解析 TMC，单事务写 Runtime、动态符号和 Schema；不激活或登录 PLC |
| `tc_hmi_ads_live_check` | 只读 | 先核对实际 XAE endpoint，再用 TcAdsDll 在线读取已配置动态符号；不写 PLC |
| `tc_hmi_binding_diagnose` | 只读 | 绑定故障首选入口；分开验证表达式、TMC 导出、Server 映射/Schema、实际 endpoint 与 ADS 在线读，并返回有序修复建议 |
| `tc_hmi_bindings` | 只读 | 清点 SymbolExpression，检查 Runtime、映射、内部符号和控件 ID 引用 |
| `tc_hmi_internal_symbols` | 只读 | 读取 type、默认值、persist 和 readonly |
| `tc_hmi_internal_symbol_set` | 项目写入 | 按 1.12 IInternalSymbolItem 契约预览/写入/删除并回读 |
| `tc_hmi_localizations` | 只读 | 合并注册语言文件、列出键并报告缺失语言值 |
| `tc_hmi_localization_set` | 项目写入 | 在已注册 locale 的最终覆盖文件中增改/删除键并回读 |
| `tc_hmi_themes` | 只读 | 读取活动主题、主题文件及项目级 ThemedResource |
| `tc_hmi_themed_resource_set` | 项目写入 | 按 1.12 项目 schema 写入每主题值并回读 |
| `tc_hmi_active_theme_set` | 项目写入 | 只允许切换到已注册主题并回读 |
| `tc_hmi_user_controls` | 只读 | 列出 UserControl、参数文档、参数和内部控件 |
| `tc_hmi_user_control_create` | 项目写入 | 按项目 Framework 创建主文件与参数文档，登记并回读 |
| `tc_hmi_user_control_parameter_set` | 项目写入 | 校验 `data-tchmi-*`/`tchmi:` 契约后增改或删除参数 |
| `tc_hmi_user_control_delete` | 项目写入 | 备份后删除两个文件、Framework 与 MSBuild 登记 |
| `tc_hmi_framework_templates` | 只读 | 列出本机 TE2000 TypeScript/JavaScript/Empty 工程模板和目标版本 |
| `tc_hmi_framework_validate` | 只读 | 检查 `.hmiextproj`、Manifest、Description、模板、主题和类型 schema |
| `tc_hmi_framework_control_info` | 只读 | 返回 Control 的属性、函数、事件和源码路径 |
| `tc_hmi_framework_attribute_set` | 文件写入 | 同步更新属性描述与受控 getter/setter/process 代码区 |
| `tc_hmi_framework_event_set` | 文件写入 | 同步更新事件描述与 `EventProvider.raise` helper |
| `tc_hmi_framework_create` | 文件写入 | 从已安装官方模板生成 native1.12 Framework Control 包骨架 |
| `tc_hmi_framework_pack` | 文件写入 | 生成并回读校验本地 `.nupkg`，不安装、不发布 |
| `tc_hmi_framework_packages` | 只读 | 交叉核对包目录、NuGet 引用、HMI 注册和 Server 虚拟目录 |
| `tc_hmi_framework_package_inspect` | 只读 | 校验包 ID/版本、Framework 目标、Manifest、控件描述和依赖 |
| `tc_hmi_framework_install` | 项目写入/硬确认 | 四处同步安装、XAE 重载回读，失败自动回滚 |
| `tc_hmi_framework_uninstall` | 项目写入/硬确认 | 保护核心包、扫描 Markup 引用、备份移动并回读 |
| `tc_hmi_runtime_info` | 只读 | 关联工程与 Engineering Server，解析真实应用 URL 并进行 HTTP 探测 |
| `tc_hmi_server_control` | 进程操作 | 默认预览；按精确 `storageDir` 后台启停/重启 Engineering Server，不构建或发布 |
| `tc_hmi_browser_validate` | 只读 | 隐藏浏览器采集运行时、网络、控件和多视口布局诊断 |
| `tc_hmi_build` | 构建写入 | 产生 `bin`，不发布、不抢 Error List 焦点 |

XAE Shell 15 的 HMI Project System 在部分版本中会返回空的 `Project.Name/FullName/UniqueName`
并且不公开 `ProjectItems`。当前兼容路径从当前 `.sln` 解析 `.hmiproj`；新增页面先保留原工程
文本，再写 `<Content Include>`，最后通过 DTE 移除并重新加入工程。任一步失败都必须恢复原
`.hmiproj` 并删除本次创建的页面，不能只根据文件写入无异常就返回成功。

## 后续工具路线

1. 控件 rename 与跨页面 ID 引用检查；当前已支持 add/update/remove 和失败回滚。
2. ADS 实时连通性与符号查询已完成直接 TcAdsDll 只读验证；后续补经 HMI Server WebSocket 的值断言。
3. TMC 驱动的离线 Automap 和动态结构体在线读取已完成；后续补增量移除陈旧映射。
4. Framework Control install/uninstall 已完成；后续补包版本升级和依赖版本范围求解。
5. 浏览器运行时基础验证已完成；后续补交互动作、认证角色矩阵和 ADS 在线值断言。
6. Engineering Server 精确启停已完成：启动必须经 XAE 工程重载下发内存配置，单独运行
   `TcHmiSrv.exe` 只会产生未配置的 404 Server；生产发布/回滚仍需目标、凭据、覆盖和硬确认。

### 映射与 SymbolExpression 实测契约

本机 `TF2000-HMI-Server/TcHmiAds/TcHmiAds.Schema.json` 明确规定 Runtime 的每个
`SYMBOLS.<name>` 必须包含：

```json
{
  "INDEXGROUP": 0,
  "INDEXOFFSET": 0,
  "TYPENAME": "BOOL"
}
```

这里的数值仅为结构示例，不是推荐地址。写入工具不得自行猜测真实 PLC 地址或类型。官方文档
说明 Server Symbol 在绑定到控件属性前必须映射；常见 Server SymbolExpression 为
`%s%ADS.PLC1.MAIN.bRun%/s%`，内部符号示例为 `%i%MyInternalSymbol%/i%`。静态绑定审查只证明：

- 表达式语法被识别；
- 指定 ADS Runtime 存在；
- 相同符号名在保存的 Runtime 映射中存在；
- 内部符号或控件 ID 在工程中存在。

它不调用 `TcHmi.Symbol.exists/resolveSchema`，因此不能把 `mapped` 报告成“在线符号有效”。
参考：<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/4995379211.html>、
<https://infosys.beckhoff.com/content/1033/tc3_hmi_process_library/17934386571.html>。

### 内部符号

1.12 的 `IInternalSymbolItem` 结构为 `value/type/persist?/readonly?`，其中项目 schema 要求
`type` 以 `tchmi:` 开头，例如 `tchmi:general#/definitions/Boolean`。内部符号属于浏览器实例，
不依赖 HMI Server；`persist=true` 表示最近值保存在该客户端并可在重新加载后恢复。

XAE HMI Project System 会在项目卸载时保存内存中的 `tchmiconfig.json`。因此自动化修改顺序必须是：

1. `File.SaveAll` 保存用户编辑；
2. 从 Solution 移除 HMI 项目；
3. 写入新的 `tchmiconfig.json`；
4. `Solution.AddFromFile` 重新加载；
5. 回读符号字段。

如果先写配置再移除项目，新内容可能被旧内存状态静默覆盖。官方结构参考：
<https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/8811965963.html>。

### Localization 与 Theme

当前 1.12 工程以 `tchmiconfig.json.languages` 将 locale 映射到一个文件或有序文件数组；
`.localization` 文件的必需字段是 `locale` 和 `localizedText`。同一 locale 注册多个文件时，项目
schema 规定后面的文件覆盖前面的同名键，因此写入工具只修改最后一个文件，不擅自重排或合并来源。
删除时未指定 locale 表示从所有已注册项目语言删除该键；工具暂不隐式创建新语言文件。

主题由 `tchmiconfig.json.themes` 注册，每个主题资源只能是 `Stylesheet` 或 `ThemedValues`；
`activeTheme` 必须指向已注册主题。项目级通用主题资源保存在
`symbols.themedResources.<name>`，包含 `type`（必须是 `tchmi:` schema 引用）、可选描述和按主题名
索引的 `values`，页面通过 `%tr%Name%/tr%` 引用。它不同于 Framework Control
`Description.json` 中声明的控件私有 `themedResources`，二者不得混用写入接口。

`tc_hmi_validate` 同时检查活动主题、主题资源文件、本地化 JSON/locale 一致性和项目级主题资源的
缺失主题值。Localization、ThemedResource 和 activeTheme 写入均遵循 SaveAll → Remove → 写文件 →
AddFromFile → 回读；由于 HMI 加载是异步的，后续独立调用允许最多 2 秒的有界项目发现重试。

### UserControl

TE2000 的 UserControl 是一对同目录文件：`Name.usercontrol` 保存 Markup，
`Name.usercontrol.json` 保存参数契约。Visual Studio 项目树里的“子文件”只是
`.hmiproj` 中 `<DependentUpon>Name.usercontrol</DependentUpon>` 的显示效果，磁盘上不存在
`Name.usercontrol\` 目录。参数名必须以 `data-tchmi-` 开头，类型必须为 `tchmi:` schema
引用。`tc_hmi_validate` 同时检查 Framework 注册、两个文件、MSBuild 从属关系、
参数必需字段和重名。

### Framework Control Project

TE2000 1.12 的 Framework Control 工程以 `.hmiextproj` 为根，`Manifest.json` 声明
`Control` module，每个控件的 `Description.json` 声明 namespace、base、Template、dependencyFiles、
themes 和 dataTypes。工具直接复用当前机器已安装的 TE2000 模板，并从当前 HMI
解决方案的 `Packages` 目录回读 `Microsoft.TypeScript.MSBuild` 和
`Beckhoff.TwinCAT.HMI.Framework` 版本，不固定包版本。

`tc_hmi_framework_create` 当前只支持 `native1.12-tchmi`，且只创建源码工程；它不会
自动加入 XAE Solution、执行 NuGet restore/pack 或发布。TypeScript 模板的
`Description.json` 会引用编译产物 `.js`，初始骨架仅有 `.ts`；校验对这一个已知
状态返回 `generated-javascript-pending` 警告，其他缺失资源仍属于错误。

Framework 属性至少包含 `name/propertyName/propertyGetterName/displayName/type`。可写属性还必须
有可解析的 setter；运行时 setter 应做 ValueConverter 转换、internal default 回退、变更检测并触发
`this.__id + '.onPropertyChanged'`。自定义事件按 `.onName` 声明，通过
`TcHmi.EventProvider.raise(this.__id + '.onName', data)` 触发。Agent 只重写带
`TwinCATAgent:Attribute/Event` 边界的代码区，避免覆盖用户实现。

NuGet pack 使用 TE2000 安装目录自带的 `bin/nuget/nuget.exe`。打包前必须结构通过且不存在
`generated-javascript-pending`；打包时排除 `.TwinCATAgent`、`bin`、`obj`，完成后回读 ZIP，确认
`runtimes/native1.12-tchmi/Manifest.json` 与 Manifest 中每个 Control 的 Description 都存在。
pack 只生成本地 `.nupkg`，不等于安装、工程引用更新或 HMI Server 发布。

安装一个 Framework Control 包必须同时完成并回读四项状态：
`Packages/<Id.Version>` 物理内容、`packages.config` NuGet 引用、`tchmiconfig.json.packages`
Framework 注册，以及 `TcHmiSrv.Config.default.json.VIRTUALDIRECTORIES` 运行时映射。
由于 XAE 卸载项目时会保存内存配置，顺序固定为 SaveAll、校验/解包、移除 HMI 项目、写三份配置、
重新加入并回读。安装与卸载均要求 `acknowledge_package_change=true`；卸载前按控件 namespace 扫描
`.view/.content/.usercontrol`，有引用时默认阻止，并把包目录移动到可恢复备份而不是直接删除。

### Engineering Preview 浏览器验证

Engineering Server 根 URL 通常是 `TcHmiSrv` 配置页，不是 HMI 应用。Agent 先匹配命令行
`--storageDir` 与当前工程，再结合实际 `--endpoint`、Server `VIRTUALDIRECTORIES` 和
`DEFAULTDOCUMENT` 找到入口；本机 1.12 工程实测入口为 `/bin/Default.html`。

`tc_hmi_browser_validate` 使用独立临时 Profile 的隐藏 Edge DevTools 会话，检查 `window.TcHmi`、
`TcHmi.Server`、主容器、View 和控件实例，采集 JavaScript exception、Console error/warning、
HTTP 4xx/5xx、资源加载失败和 WebSocket frame error，并报告重复控件 ID、文档尺寸和视口溢出。
它不执行点击和 Symbol 写入，不能代替登录权限、业务交互、ADS 在线值及生产发布验收。
