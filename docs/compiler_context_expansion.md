# 编译上下文扩展与边界

不修改用户 PLC、不启动额外 XAE、不改安装配置。允许绑定已有 XAE 的只读配置核对。

## 已接入语义检查

- 项目 FB 的基础单继承字段合并、局部优先、FINAL 基类拒绝及循环检测。
  IMPLEMENTS、多态派发和完整访问修饰符尚未实现。
- 库限定函数/类型通过 library-evidence 的实际 Namespace、LibraryName、EffectiveVersion
  匹配 library-signatures。库名不作为命名空间替代；歧义或版本不符不能放行。
  完整跨库 GVL 查找和嵌套库可见性尚未实现。
- 实现区编辑器本地 define/undefine、defined/hasvalue、NOT/AND/OR、括号及嵌套条件分支。
  NOT 优先于 AND，AND 优先于 OR；全部操作数都必须具有证据，不用短路掩盖未知宏。
  表达式上限 8192 字符、递归上限 32 层，超限返回 unknown 并保留原文。
  声明区也按编辑器本地宏选择分支；声明/实现的本地宏不互相泄漏。
  语法、语义、依赖扫描及写前质量审核共用选择结果，保留源码行列位置。
  未知条件不合并分支，不将未知 GVL 声明误报为成员缺失。条件枚举成员和条件别名不放行。
  完整本地/父声明中的 defined(variable: name) 支持肯定存在，hastype 支持已确定的基础类型，
  方法局部变量优先。不在已知声明内的变量仍可能属于全局/继承作用域，不能据此判不存在。
  这些查询仅允许用于实现区，声明区不支持；别名类型比较仍要求额外解析。
  缺少项目全局定义证据不猜 FALSE；defined(type/pou)、hasattribute、完整跨作用域查询尚未实现。
- DUT STRUCT/UNION、枚举及别名的 memory_layout 返回固定字段偏移、填充和总大小；未知属性/目标位宽不猜。

## 可执行计算核心（与实际工程证据分开）

compiler_context.layout_type 支持基础类型、字符串、固定数组、子范围、限定名别名及嵌套 STRUCT/UNION 快照。
支持源码 pack_mode=0/1/2/4/8 和显式目标指针位宽；嵌套 DUT 保留自己的 pack，不继承外层 pack。
UNION 成员同偏移；数组保留元素尾部对齐。FB 含隐式成员，拒绝只按可见字段计算。
枚举检查字面量/进制、自动递增、底层整数范围、重复名称和默认成员；复杂常量表达式仍为 unknown。
结果 layout_verified 始终为 false，表示仍需与工程的实际编译布局对比。

compiler_context.version_requirement 比较完整四段版本，缺少/不合法输入返回 unknown。
target_version_requirements 汇总声明区条件编译、声明/实现长日期以及依赖的最低版本要求。
target_compatibility_verified=false 明示未核实目标兼容性；尚未接入可靠的实际编译器版本读取。
现有 get_target_tc_version 有本机版本回退，不能用作当前工程编译器的通过证据。
禁止把候选 JSON 自报版本当成可信 XAE 上下文。

## 当前 XAE 编译选项只读证据

compiler-settings 只允许绑定 XAE PID 和精确三段 PLC 根路径，走 native bridge，不回退到猜测工程。
读取 TreeItem/IECProjectDef/CompilerSettings；核对 PathName、解决方案及连续两次读取的一致性。
只返回 ReplaceConstants、UTF8Encoding 和项目 CompilerDefines 子集。重复字段、异常布尔、缺失字段
不会变成 false。宏值解析保留引号中的逗号，不支持的表达式保留 unknown。
已在 PID 19584 的 TwinCAT Project2 当前工程只读验证该 XML 形状与新通道；未保存或改动工程。

常量传入非字符串 VAR_IN_OUT CONSTANT 时按需读取一次真实 ReplaceConstants：false 继续类型检查，
true 拒绝该引用存储用法，unknown 保留证据缺口。普通可写 VAR_IN_OUT 仍禁止常量。
该选项不影响按次授权，也不能证明运行地址、生命周期或完整编译正确。

本接口未提供系统/活动变体宏与实际编译器版本。project_defines_complete 只表示项目字段可解析，
effective_defines_complete 始终 false，禁止据此将未列出的宏判为未定义。
本机 TcSmItem.xsd 的 CompilerDefines/EnableImplicitDefines 是系统工程配置字段；不能把磁盘值
或项目版本号替代当前生效的系统宏或编译器版本。

## 官方依据

- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529795979.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/11951162891.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529203339.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/3539428491.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529746059.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529415819.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529504395.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529421195.html
- https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2533017227.html
- https://infosys.beckhoff.com/content/1033/tc3_userinterface/3434440203.html

测试覆盖官方布局例子、缺少位宽、FINAL/循环、限定名版本匹配、本地条件分支及未知全局定义。
这些是有边界的实现，不是完整 TwinCAT 编译器，也不产生写入或上线授权。

## 继承方法：完整目录证明后再查基类

read-pou 返回 member_catalog，递归枚举 FB 内组织文件夹，不读取属性访问器代码。
枚举异常、未知节点或预算超限均返回 complete=false；不能以空列表证明成员不存在。
仅当子类完整目录没有同名成员，才向已解析基类查找。子类同名成员读取失败、
目录不完整或歧义时不回退；继承 PRIVATE 方法被拒绝。目录包含相对成员路径，
可读取文件夹内成员。基类版本/工程定位继续沿用原有精确项目范围。
测试：test_inherited_member_catalog.py。

## TCP/IP 公开结构（2026-09-12 实际失败记录回归）

仅在实际 Tc2_TcpIp 库签名含对应 Type 时附加 T_HSOCKET / ST_SockAddr 的 InfoSys 公开字段。
T_HSOCKET 保持 opaque_type：只验证公开成员类型，不制造完整声明、内存偏移或精确版本 ABI 证明。
嵌套地址类型必须来自同一已解析库版本，同名项目类型不能替代库成员类型。

结构体与标量比较/赋值返回 semantic-type，并定位实际实现区行号；hSocket.bInit 返回 semantic-member。
这类明确代码错误不进入 semantic_failures 能力缺口计数，原重复失败保护仍保留。
GENERATION_RULES 在生成前说明不得将 T_HSOCKET 当整数，句柄值也不能单独证明连接在线。

只读重放当前工程两次失败候选：原先 DINT -> T_HSOCKET 的 incomplete 现为 invalid，分别定位实现区
第 3/40 行；仅在内存副本中将比较改为 hSocket.handle 后，该语义缺口消失。完整写前质量门禁仍未通过
（缺少注释/周期调用等），没有将部分语义通过称为整程序验收。未写入用户 PLC、未编译或上线。

官方接口：
- https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84182539.html
- https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84179467.html
