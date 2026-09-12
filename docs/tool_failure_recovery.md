# 工具失败恢复规则

2026-09-08：针对 XAE 对话中的误报和重复试错。

- 成功判定检查非空 error，仍保留 verified=false、真实错误、拒绝及诊断缺失门禁。
- PLC patch 在审查时计算完整预期内容，写后逐字回读（只容许编辑器行尾规范化）。
  追加时新文本包含旧文本不再误报；唯一匹配不成立则不写。写入但验证失败不得原样重放。
- XAE 诊断保留 raw_error_level 和 severity_inferred。构建失败不再把所有有工程路径的
  警告提升为错误；低级别项只有明确编译器代码才推断为错误。错误详情未暴露时保持待确认。
- 当前轮确定性错误相同参数被拦截；瞬时忙碌不缓存。成功写入使前置状态失效，下一轮重新评估。
  缺少 HMI 项目时阻止同项目连续 info/structure/catalog 查询，不隐式创建。
- 权限拒绝仍保持拒绝，不以 auto 或重试绕过；后续用户明确授权重新走审批。
- PLC 常用语法和路径规则已注入后台提示：strict 放 TYPE 前、枚举成员赋值、转换函数、
  范围和注释、引用前核对声明、原样复用 plc_find 路径。不能保证模型从此不再生成错误；
  本地规范门禁和编译仍是必需验证。
- tc_hmi_ads_symbols 默认 both，分别提供 static_symbols、dynamic_symbols。
  动态目录仅检查 default Server 配置，返回真实键对应的 page_expression；remote 明确未检查。
  不以静态 symbol_count=0 推断动态映射缺失，也不调用空 dynamic_symbols_set 进行查询。

验证不修改用户项目、不部署、不启动 PLC。VSIX 修复需用户关闭 XAE 后再安装验证。
