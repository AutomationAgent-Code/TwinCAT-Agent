# Official call/operator coverage — 2026-09-12

This expansion follows the [Beckhoff operator index](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528853899.html).
It is source-level validation, not a complete TwinCAT compiler or runtime proof.

| Family | Added handling | Boundaries |
| --- | --- | --- |
| Clock | `TIME()` | Zero arguments, TIME result; no simulated time or absolute timestamp |
| Storage size | `SIZEOF(variable/type)` | Fixed scalar, string, array and supported explicit DUT layouts; no invented pointer/FB layout |
| Native size | `XSIZEOF` | Symbolic `__UXINT`, 4026 requirement recorded; target version/width remain unverified |
| Selection | `MIN MAX LIMIT SEL MUX MOVE` | Positional arguments, arity and selector types; mixed-type overloads stay unverified |
| Numeric | `ABS EXPT TRUNC TRUNC_INT ACOS ASIN ATAN COS SIN TAN EXP LN LOG SQRT` | Numeric input types; no proof of runtime domain/range, overflow or target rounding |
| Bit shifts | `SHL SHR ROL ROR` | Operand/count types, rotations restricted to bitstrings; no invented target overflow behavior |
| Memory library | `MEMSET MEMCPY MEMMOVE MEMCMP` | Must exist as a Function in actual referenced Tc2_System signatures; official public signature fills name-only or matching-input/missing-return evidence, never contradictory live fields |

## Official sources

* [TIME()](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529365899.html)
* [SIZEOF](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528896907.html), [XSIZEOF](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/13614027275.html)
* [MEMSET](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31042699.html), [MEMCPY](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31041163.html), [MEMMOVE](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31044235.html), [MEMCMP](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31039627.html)

## Remaining explicit boundaries

Existing ADR, array-bound queries, conversions and infix arithmetic remain in their
own handlers. This change does not implement IL/CFC/FBD execution, BITADR,
INDEXOF, __NEW/__DELETE allocation, interface query/reflection operators or all
vendor libraries. Missing layout or overload evidence still blocks the candidate;
it is not classified as a definite source error. Qualified built-in aliases and
named arguments for the added compiler operators are not assumed valid.

Memory signatures do not prove address lifetime, online-change validity,
overlap, writable storage or dynamic byte-count safety. Missing parameters,
constant null addresses, out-of-range fill byte and constant lengths beyond a
known directly addressed variable are checked. MEMCPY overlapping regions are
not proven safe; use the documented MEMMOVE when overlap is intentional, with
appropriate application review. This is not blanket memory-safety approval.

Full compiler equivalence is deliberately not claimed. Coverage tests include
invalid arity/types, unknown names, wrong library identity, conflicting live
signatures, target-dependent results and the full dependency-resolution path.
# TE1000 官方目录复核（2026-09-12）

入口：https://infosys.beckhoff.com/content/1033/tcinfosys3/4327303819.html

该入口覆盖整个 XAE，不是可直接导入的 PLC 语义规范。实施时按 PLC 语言、PLC 库、工程配置分别取证；不能把库目录中的函数全注册为无依赖内建符号，也不能把 PLC 写前检查宣称为 Safety、运动或运行时验证。

本轮追加回归：一维定长数组 `ADR(a[i])` 按已知索引后的剩余字节容量检查；`SIZEOF(...)` 长度参与静态越界检查；MEMSET/MEMCPY/MEMMOVE 的直接 ADR 目标受 CONSTANT 写保护。动态索引、多维剩余范围及未知 ABI 布局不猜容量。容量检查不是实际地址、生命周期或运行安全证明。

依据：[MEMSET](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31042699.html) 将 n 定义为从起始地址写入的字节数，并警告非法内存访问风险；[SIZEOF](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528896907.html) 返回变量或类型占用的字节数。
