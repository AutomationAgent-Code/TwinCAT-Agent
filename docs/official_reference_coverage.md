# 官方编程参考：后端覆盖范围

入口：https://infosys.beckhoff.com/content/1033/tc3_plc_intro/25282000754359667083.html

该页是目录，不代表离线预检已实现全部 TwinCAT 语义。2026-09-12 本轮核对了
Identifier、Operators、Data types，以及下面实际落地的子章节。

| 官方章节 | 后端行为 | 边界 |
| --- | --- | --- |
| [CONSTANT](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/11982693643.html) | 检查直接赋值、FOR 控制变量、输出接收及可写引用参数；本地常量的成员/数组元素沿用只读属性；普通常量声明必须初始化 | 不把常量指针的指向对象误判为常量；未全面解析跨库只读属性 |
| [MOD](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528880779.html) | 已知基础类型中的非整数/位串操作数报明确错误 | 未知别名仍需接口证据，不猜测 |
| [DIV](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528875403.html) | 除法/MOD 的十进制整数字面量零除数被安全检查阻断 | 这是运行风险检查，不声称 TwinCAT 必然编译失败；不猜动态除数，不模拟目标硬件 |
| [VAR_IN_OUT](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528771211.html) | 基础引用存储类型检查、只读字符串参数区别于普通赋值 | 参见 inout_preflight_contract.md |

生成前的 syntax_rules 升级为版本 3，包含官方来源和上述写前要求。
项目不可用时仍保留这些基础规则。常量声明初始化与实现区写入分开处理。
测试：tests/test_official_reference_rules.py、tests/test_inout_preflight.py。

未覆盖：完整命名空间/继承、所有运算符重载、动态值与生命周期、目标位宽相关溢出、
日期范围和版本要求、完整 pragma/库内部语义。编译证据继续为独立字段，
离线检查不能替代 XAE 构建，也不产生上线授权。

## 第二批：声明与引用字符串

新增 declaration_contract.py，经公共 syntax_findings 接入写前质量检查。
变量标识符的字符集、首字符和大小写重复检查依据
[Identifier](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529913227.html)。
不人为限制名字长度，不把双下划线建议升级为本检查的硬错误。
普通 CONSTANT 缺少初始化与 VAR_IN_OUT CONSTANT 携带初始化均为明确声明错误。
条件编译分支不混合判重，交给预处理证据。字符串/注释先屏蔽，避免将其中的分号和赋值符当代码。

可写引用参数的显式 STRING(n)/WSTRING(n) 实参不得短于形参；较长实参允许。
只读引用字符串不套用容量限制。符号长度、默认长度和别名长度仍需后续支持。
测试：test_declaration_contract.py 和 test_inout_preflight.py。

## 全目录覆盖台账（不是完整编译器认证）

| 官方目录 | 当前状态 / 后续缺口 |
| --- | --- |
| 编程语言与编辑器 | 部分 ST 与声明/实现分离；其他语言不由 ST 解析器验证 |
| 变量 | 部分作用域、常量、参数、数组；完整 AT/持久化/外部变量约束待补 |
| 运算符 | 部分表达式、引用、类型转换；完整重载与目标位宽语义待补 |
| 操作数 | 部分字面量与对象解析；完整日期、编码及命名空间待补 |
| 数据类型 | 常见类型与部分库接口；完整继承、别名、子范围及开放数组待补 |
| 全局数据类型 | 实际工程依赖解析；完整跨库限定名待补 |
| 对齐 | 未做 ABI/内存布局证明 |
| Pragmas | 识别部分能力缺口；未实现编译器预处理 |
| 标识符 | 基础变量名字与同作用域重复检查；完整限定名待补 |
| 遮蔽规则 | 部分局部优先；完整作用域解析待补 |
| 关键字 | 部分语法识别；官方完整保留名检查尚未落地 |
| FB_init/reinit/exit | 未实现完整生命周期契约 |
| 编译错误与警告 | XAE 诊断证据通路；不能由离线检查冒充编译结果 |

只有实际存在的规则和回归测试才记作部分覆盖；未支持项保持显式边界。

## 第三批：数字字面量、生命周期接口及证据分类

- [Numeric Constants](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529316491.html)：
  整数解析统一支持十进制下划线、二/八/十六进制、显式整数类型和进制组合。
  这些字面量参与既有范围、数组下标和零除数规则；不计算目标位宽相关的中间溢出。
- [Typed Literals](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529332619.html)：
  检查整数显式类型自身的取值范围，而不只检查赋值目标。
- [Lifecycle interfaces](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/5044757003.html)：
  检查 BOOL 返回值、FB_init/FB_exit 必需的 BOOL 输入和禁止增加的输出/传递参数。
  在 FB_init 内拒绝 SUPER^.FB_init，不把 SUPER^.FB_reinit 当成同类错误。
  完整继承初始化顺序、在线变化与内存生命周期仍未验证。
- syntax-unsupported 归类为 incomplete，而不是代码错误；仍不生成批准或编译成功证据。

测试：test_integer_literals.py、test_lifecycle_contract.py。

## 第四批：数组声明

依据 [固定数组](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/8825253771.html)
和 [变长数组](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/8825257611.html)：
检查字面量上下界顺序和 DINT 范围；ARRAY[*] 仅允许 VAR_IN_OUT，全部维度为 *；
拒绝 BIT 数组元素和 POINTER/REFERENCE TO BIT。符号边界暂不在此层求值。
测试：test_array_declaration_contract.py。

另补：非字符串常量传给只读引用参数时，需要 Replace constants 编译选项证据；
缺少证据返回 incomplete，不伪装成确定代码错误，也不直接批准。

## 第五批：数组边界运算符与字符串默认容量

- LOWER_BOUND/UPPER_BOUND 内置识别：两个位置参数、数组实参、整型维度、
  字面量维度范围；无需错误地查找同名库函数。运行时实际边界仍不被静态证明。
- [STRING](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529410443.html) 和
  [WSTRING](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529437323.html)：
  默认容量 80，方括号容量与圆括号容量统一；ASCII 字面量长度和可写引用容量使用同一解析。
  长字面量检查是防止截断的数据质量策略，不声称 TwinCAT 一定拒绝编译。
  UTF-8 多字节、IEC 转义、非 ASCII WSTRING 范围尚未在此层完成。

测试：test_array_bound_operators.py、test_string_capacity_contract.py。

## 第六批：审计缺口扩展

- 修复数组参数的秩和元素类型漏检。开放形参数组可接收对应秩/元素类型的固定数组；
  不同固定边界先标记未确认，不伪造兼容性结论。
- 支持普通 DUT 别名链（基础类型、字符串、数组、字面量子范围），16 层上限和循环检测。
  带默认初始化的别名、复杂限定名和完整继承仍不支持。
- 子范围字面量上下界及赋值范围检查；符号边界仍需常量解析。
- 实现区标准 Region 编辑器指令可跳过；条件/属性 Pragma 不因此自动放行。
- 对官方表中明确的 ST 类型、作用域与操作符关键字执行变量名拒绝；不将 IL 专用助记符
  一律套用到缺少语言上下文的声明，不声称完整关键字覆盖。
- DATE/DT/TOD 字面量校验日历、时钟与官方上下限，声明标准长名称归一化；
  长日期纳秒范围、实际编译器版本闭环仍需实现。
- 名称遮蔽、长日期版本和对齐要求进入生成前提示，但完整名称解析/内存布局计算
  **尚未实现**。提示规则不能计作可执行验证覆盖。

测试：test_type_contract_expansion.py、test_date_literal_contract.py。
