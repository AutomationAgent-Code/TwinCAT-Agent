# 官方规则核对记录（2026-09-08）

依据用户提供的官方规则入口和 PLC 编程参考，核对用户提交的 35 条 SA 错误及 3 条 C0555 编译警告。
此记录是规格核对，不表示自有 TCSA 已实现或通过官方规则。

## 配置必须纳入对比

[Rules](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/3471348875.html)
明确区分禁用、输出为错误、输出为警告；部分规则还有参数和阈值。
4026.20 起预编译消息的显示还受全局开关及逐规则开关控制。
“Importance”不是用户配置的 Severity。比较结果必须同时记录版本、源码状态、检查范围、
启用规则、严重级别、参数、属性和抑制设置；不得仅凭编号相同推断实现一致。

## 本次差异

| 规则 | 官方含义/条件 | 对当前 Agent 的影响 |
|---|---|---|
| [SA0038](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20911342347.html) | 检测 POU 内部对 VAR_OUTPUT 的读取，示例包含方法内读取 | 修正此前错误的写前指导；输出用作内部状态仍需检查 |
| [SA0027](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20909670795.html) | 名称冲突覆盖应用和引用库；qualified_only 枚举豁免 | strict 不等于 qualified_only；不能简单禁止所有同名枚举成员 |
| [SA0033](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20909675147.html) | 声明后未在编译代码使用的变量 | 当前局部变量文本计数覆盖不足 |
| [SA0043](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20911353227.html) | 全局变量只被一个 POU 使用 | 需跨对象引用分析；针对 GVL_Hmi 的处理还应核对外部使用，不能自动删除 |
| [SA0040](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20911346699.html) | 检查潜在零分母；示例认可从非零常量赋值得到的分母 | 字面量零检测不足；文档不足以单独解释本工程转换表达式下为何仍报警，需同版本实测 |
| [SA0062](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20911320587.html) | 检查运行时恒值表达式，含布尔表达式和赋值后恒值条件 | 需常量传播和表达式推理，非单纯匹配 TRUE/FALSE |
| [SA0172](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20909714315.html) | 检查可能越出数组边界的索引 | 需推导实际访问位置的索引范围；注释“0..2”不是范围证明 |
| [SA0178](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20909718667.html) | 可配置复杂度上限，默认20 | 当前 TCSA 默认15且算法近似，数值不可直接比较 |

## 复杂度计分证据

[Cognitive complexity](https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/20908725643.html)
给出的例子表明：控制流影响计分，嵌套增加权重；连续相同布尔运算符构成一条链，
运算符切换形成新的链；NOT 不单独增加复杂度；SEL、MUX、JMP 也计分，RETURN 和 EXIT 不增加。
只按行首关键字统计不能复现该指标。公开示例可作为后续测试规格，但不是完整编译器算法证明。

## PLC 编程语义

[Reference Programming](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/25282000754359667083.html)
是语法及语义参考入口，而不是另一个 SA 规则表。

[Enumerations](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529504395.html)
区分 strict 的类型检查与 qualified_only 的限定访问；修复枚举冲突前应核对声明属性。

[STRING](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529410443.html)
说明 STRING 容量按字节计。UTF-8 下字符数不等于字节数，字符串处理函数还须支持 UTF-8。
用户提供的 C0555 应结合工程“UTF-8 Encoding for STRING”设置核对；源码文件保存为 UTF-8
本身不能证明 PLC STRING 编码配置正确。

## 引擎接入验证（2026-09-08 后续）

`static_rules.py` 已加入独立的源码引用、输出读取、枚举冲突、有限区间传播和复杂度计分。
上表记录的是接入前的差距；当前支持范围以 `docs/tcsa_static_analysis.md` 和工具返回的
`coverage` 为准。没有修改 PLC 程序或 XAE 规则配置。

用用户报告对应的当前已保存 7 个 PLC 对象检查，匹配如下（数量一致不等于算法全面等价）：

| 规则 | 用户官方报告 | 当前自有检查 |
|---|---:|---:|
| SA0027 | 2 | 2 |
| SA0033 | 8 | 8 |
| SA0038 | 2 | 2 |
| SA0043 | 19 | 19 |
| SA0040 | 1 | 1（相同实现行118） |
| SA0172 | 1 | 1（相同实现行104） |
| SA0178 | 1，得分57 | 1，得分59；差异保留 |
| SA0062 | 1 | 0；本工程未复现，规则已有独立示例测试 |
| SA0075 | 未列出 | 2；不能从报告缺失反推该规则启用状态 |
| C0555 | 3 | 不作为 TCSA 规则执行 |

官方报告对应的源码时间点/未保存状态及完整规则配置未获取，以上仅是对比线索。
当前默认输出36条 warning。可按用户明确配置将相应 SA 规则设置为 error/off；不会为了
凑相同输出而自动禁用 SA0075，或把全局变量初始值当作永远不变来制造 SA0062。
