# VAR_IN_OUT 写前接口检查

依据 Beckhoff 官方接口说明：
https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528771211.html

预检区分按值输入与按引用输入输出：基础数值/位类型的 VAR_IN_OUT
不允许通过普通赋值的隐式转换混用存储类型。普通 VAR_INPUT 的赋值兼容规则不变。
声明解析保留 CONSTANT 标志，局部常量不得传给可写 VAR_IN_OUT。

VAR_IN_OUT CONSTANT 的 STRING/WSTRING 允许字符串字面量，且不按形参长度
执行普通赋值的截断检查。可写 VAR_IN_OUT 仍要求变量。

范围限制：该检查不证明所有引用可写性、属性返回引用的生命周期、别名等价、
复杂数组兼容或编译选项相关的非常量字符串以外的常量传递规则。它不替代
TwinCAT 编译；也不新增保存、编译、上线或运行时写入授权。

回归测试：tests/test_inout_preflight.py。测试使用隔离候选与接口声明，不操作 XAE。
