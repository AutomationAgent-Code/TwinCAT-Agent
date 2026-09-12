# PLC 读取错误分类

代码读取针对 POU、FB、DUT、GVL、接口及其成员，不读取 PLC 工程属性。
`PLC1` 是项目名，`PLC1.plcproj` 是工程文件，都不应拿来反复尝试 `plc_read`。
工程信息用 `tc_project_info`；代码对象用 `plc_find`/`plc_tree` 定位，原样传回 COM path。
磁盘索引的逻辑路径与 `TIPC^...` COM 路径不能混用。

错误前缀：

- `project_not_code_object`：工程文件或经项目清单确认的项目名被用于代码读取。
- `plc_project_unavailable`：未发现可用 NestedProject，或所选 PLC 的 NestedProject 不可用。
  可能加载未完成、被禁用或加载失败，但这本身不证明 Disabled。
- `invalid_object_path`：路径不在当前可用 PLC 工程内或不能解析为指定对象。
- `code_object_not_found`：项目可访问，但没有找到指定对象或成员。

Agent 普通、智能及批量请求在规范化前拦截工程文件名。
公共 COM 单对象读取在失败后做一次新鲜项目清单诊断，不重复读对象、不猜路径、不切项目。
PowerShell 对象定位在搜索前检查 NestedProject 可用性。
成功的读取不额外增加 COM 查询；磁盘索引读取仍只证明已保存源码存在，不证明 XAE 项目已加载。
诊断通道本身失败则保留原始错误，不用空清单假装项目不存在。

当前修复不自动 Enable/Reload、不修改工程文件；禁用和加载失败的真实原因仍需当时 XAE 的加载诊断。
