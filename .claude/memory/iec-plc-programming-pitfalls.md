---
name: iec-plc-programming-pitfalls
description: "IEC 61131-3 PLC code common mistakes — comment syntax, TON pattern, counter overflow, CiA 402 statusword"
metadata: 
  node_type: memory
  type: project
  originSessionId: 90fba888-d289-4c9a-90d4-5a69e40b105f
---

## IEC 61131-3 / TwinCAT PLC 编程易错点

### 1. 注释语法：必须用 `(* *)` 或 `//`

TwinCAT 只识别 IEC 61131-3 标准注释。`{# #}` 和 `{// }` 是 Jinja2 模板语法，PLC 编译器不认识，**会导致编译错误或注释后的代码被忽略**。

### 2. TON 闪烁模式：不能 `IN := NOT Q`

```pascal
(* ❌ 错误 — Q 只维持 1 个周期(10ms)，肉眼不可见 *)
tonBlink(IN := NOT tonBlink.Q, PT := T#500MS);
blinkQ := tonBlink.Q;

(* ✅ 正确 — Q 触发时手动翻转，标准 50% 占空比 *)
tonBlink(IN := TRUE, PT := T#500MS);
IF tonBlink.Q THEN
    tonBlink(IN := FALSE);
    blinkQ := NOT blinkQ;
END_IF
```

**Why:** `IN := NOT Q` 下一周期 Q 立即归零，外部永远看不到 TRUE 状态，输出只有 1 个周期脉宽。

### 3. UDINT 循环计数器有溢出风险

```pascal
(* ❌ 497 天后溢出归零，MOD 触发一次伪 tick *)
cycleCount : UDINT;  // 0~4,294,967,295
cycleCount := cycleCount + 1;
tick := (cycleCount MOD 100 = 0);

(* ✅ 自复位计数器，永不溢出 *)
tickCounter : USINT;  // 0~255
tick := FALSE;
tickCounter := tickCounter + 1;
IF tickCounter >= 10 THEN
    tickCounter := 0;
    tick := TRUE;
END_IF
```

### 4. CiA 402 StatusWord 必须查 bit 0,1,2

`16#0021` (bit0+5) 不够：Ready to switch on (bit0) + bit5 不代表到了 Operation Enabled。正确检查 `16#006F = 16#0027` (bit0+1+2+5+6)。

**How to apply:** 写 PLC 代码时用 `(* *)` 注释，TON 用 `IN := TRUE + IF Q THEN` 模式，大计数器用自复位 USINT。
