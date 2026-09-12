# PLC 写前依赖与结果契约

Agent 普通写入/补丁先完整读取当前对象声明和实现，再按候选内容执行规范审查及有界的实时依赖检查。
截断的源代码不能作为完整审查基线。创建对象、成员和属性访问器携带实现时也执行依赖检查。
新建代码引用外部 GVL/枚举时，应传递已发现的同 PLC 精确 COM 父路径；不能确定范围时先创建空骨架，再精确读写。

## 当前执行覆盖

- `GVL_` 限定名的直接成员：同一 `TIPC^PLC^NestedProject` 内唯一查找，COM 精读声明；缺成员阻止写入。
- 简单标量赋值：读取 `E_` 枚举声明，拦截已知整数变量直接赋给枚举。
- 实现区出现 VAR/END_VAR，整数 `TYPE#(表达式)`：阻止写入。
- 中文等非 ASCII STRING 字面量：提示核对实际编译器编码，不把编码未知当作编译错误，也不自动改编码。
- 缺失、重名、跨 PLC、截断、COM 查询失败和超出依赖预算：返回上下文未确认，阻止相关候选写入；不猜测对象不存在。
- 注释和字符串中的伪引用不参与依赖匹配；局部同名变量不当成 GVL。

这不是完整 ST 类型系统：不解析库命名空间、继承、指针、数组表达式、任意函数返回类型或嵌套结构成员。
没有这些检查结果不代表对应语义通过。生成上下文提供已保存库与编译器选项线索，但未保存内容及活动平台必须另行确认。
本层是 Agent 编辑入口的约束，不声称覆盖低层 CLI/COM、模板导入或所有生成器。

## 结果解释

- `failure_stage=pre_write_review, written=false`：规范/依赖拒绝，未发写指令。
- `failure_stage=com_write, status=uncertain, written=null`：调用异常，不能证明没有写入；`retry_safe=false`。
- `failure_stage=post_write_readback, written=true, verified=false`：写入已提交，但回读异常或不一致，先只读核对，不盲目重发。
- `verified=true, compiler_verified=false`：仅代码保存回读成功；仍需正确平台下编译验收。

补丁比较完整候选区域，允许编辑器行尾规范化；追加操作不再要求旧片段消失。
编译、激活、登录和启动保持独立权限边界。本检查不连接 ADS，不切换平台、不构建或部署。

回归：`python -m pytest tests/test_plc_write_context.py tests/test_tool_failure_fixes.py -q`
