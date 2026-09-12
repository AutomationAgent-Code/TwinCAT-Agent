# HMI 子项增删免重载验收

目标：使用官方工程接口添加/删除子项，XAE 工程保持加载，相关配置同步，
逐类测试后才对外声明支持。不以文件存在/消失或 COM 返回 true 作为完成标准。

## 验收矩阵

| 子项 | 必须同步的关联数据 | 本轮状态 |
| --- | --- | --- |
| 空文件夹 | 物理目录、树节点、Folder Include；非空目录禁止递归删除 | 原生删除后 Folder Include 残留，未完成 |
| View | Content Include、views、页面源码 | PID 18788，两轮增删通过 |
| Content | Content Include、content、页面源码 | PID 18788，两轮增删通过 |
| UserControl | 主文件、参数 JSON、DependentUpon、userControls、生成 Schema | PID 18788，两轮增删通过 |
| CodeBehind JavaScript | 源码、Content Include、dependencyFiles | 原生删除后 dependencyFiles 残留，未完成 |
| JavaScript | 源码、Content Include、dependencyFiles | 待免重载闭环 |
| CSS | 源码、Content Include、dependencyFiles Stylesheet | 待免重载闭环 |
| JavaScript Function | 源码、function.json、DependentUpon、userFunctions、dependencyFiles | 待免重载闭环 |
| TypeScript / TS CodeBehind / TS Function | 本机官方模板、编译产物路径、依赖登记、配套描述文件 | 尚未支持，不计入上述八类通过率 |
| 主题及其他已安装 Item Template | 先区分应用项目和 Framework 扩展项目，再定义对应登记契约 | 待分类，不宣称全覆盖 |

之前八类原生添加的实测不能替代本次双向闭环；之前靠重载清理配置的删除不算通过。
不包含删除整个 HMI 工程、卸载 NuGet 包或更改 ADS/PLC 运行状态。

## 每类测试过程

1. 固定当前 XAE PID、解决方案、工程、Framework/Engineering 版本。
2. 专用测试目录预览，检查重名、引用、未保存内容与路径范围。
3. 用本机官方模板添加；核对节点、文件、工程登记和各类配置。
4. 同一加载会话内删除；核对上述数据均已清除，没有卸载/重新添加工程。
5. 同名再创建，验证没有残余注册造成冲突；测试结束删除本轮自建对象。
6. 验证不影响启动页/业务页面；记录未做的浏览器或构建检查，不把文件验证冒充运行验证。

创建源码已新增 `saved_state_evidence`：配置、配套文件或适用的生成 Schema 任一缺失时，
返回 incomplete / verified=false / retry_safe=false；不能自动再次创建。
该检查只读已保存内容，描述文件能解析 JSON 不等于安装版本 Schema 已验证。

删除回读也已统一到 `saved_state_evidence`：对照实际事务备份，检查目标文件、
工程及配置登记被移除，并核对其他工程登记、其他配置、脚本依赖顺序及其他
UserControl Schema 定义保持不变。整个 Schema 文件丢失不能算清理成功。
回读失败只返回 incomplete，不另发删除、修复或重载命令。
该读盘检查明确返回 `live_hierarchy_verified=false / no_reload_verified=false`，
不能替代 VSIX 中的真实层级和生命周期验证。

## 无重载约束

页面创建已切换为官方语义路径：`AddView/AddContent -> AddControl -> ChangeAttributes`。
预检阶段只构造内存候选并用当前安装 Schema 校验，apply 阶段不复制页面源码、不调用
`Solution.Remove/AddFromFile`。普通文件查找要求 `.hmiproj` 中恰好一个精确 Include，
不会用 basename 或 `EndsWith` 命中嵌套同名文件。

- 普通增删不使用 Solution.Remove/AddFromFile，也不以隐藏重载作为降级路径。
- 异常后先报告实际状态和备份，不能自动重载来掩盖未同步。
- 删除前要走完整引用/所有权审查和快照门禁；只读 QueryDeleteItems 不构成删除授权。
- 源码删除路径已切换为完整生命周期，不再自动重载或回滚重载；安装后端仍未更新。
- 旧 repair_orphan 请求现在拒绝，要求另行审查离线修复；不通过孤立登记修复暗中重载。
- 首次更新 VSIX 需要用户保存并关闭 XAE。这是加载新版工具的前置条件，不是每次
  子项操作的一部分。不得自动关闭用户工程，不更改模型配置或会话数据库。

2026-09-09：旧 VSIX 阻点已解除。用户关闭 XAE 后完成管理员 DLL 更新及 `/setup`，
新 XAE PID 18788 的 QueryDeleteItems 探测成功且目标精确匹配。尚未执行删除；
随后通过外部 PowerShell 的 OLE IServiceProvider.QueryService(SVsSolution) 同样取得
完整删除接口，已在源码接入，不再经过抛异常的 DteProject.Object，也不依赖 VSIX 执行删除。
当前完整报告：`outputs/hmi-no-reload-files-20260909.json`。View/Content/UserControl 两轮
通过；CodeBehind 残留依赖时报 incomplete 并停止，未清理业务对象、未重载。
另一个文件夹测试的节点/目录已删除但工程登记仍在：
`AgentNativeTest/NoReloadRt20260909_folder`，备份
`.TwinCATAgent/backups/native-delete-6b7f7fff2d3142eba824f98ce7450cd3`。
CodeBehind 残留为 `AgentNativeTest/NoReloadFiles20260909_codebehind_js.js`，备份
`.TwinCATAgent/backups/native-delete-ec1d37ef54974fa790a0db4649dadc86`。
这两项需继续解决，不能通过再次删除不存在的文件来掩盖；其他三类尚未完成本轮矩阵。
