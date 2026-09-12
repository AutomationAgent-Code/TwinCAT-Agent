# FB_CylinderDualOutput（SPT SOLID 组件）

双电磁阀气缸，伸出/缩回各一个输出并互锁。**来源**：[SPT Libraries · Cylinder · Dual Output](https://beckhoff-usa-community.github.io/SPT-Libraries/SPT%20Training/Components/Examples/CylinderDualOutput.html)

## 依赖模板
`requires: [spt-cylinder-single]`（借其 `I_Cylinder` 契约，并递归带入 `spt-digital-output`）

## 用法
```iecst
VAR
    fbExt : FB_DigitalOutput(Output := bExtVar);
    fbRet : FB_DigitalOutput(Output := bRetVar);
    fbCyl : FB_CylinderDualOutput(ExtendOutput := fbExt, RetractOutput := fbRet);
END_VAR

fbCyl.Extend();    // ext=1, ret=0
fbCyl.Retract();   // ext=0, ret=1
```

## 设计要点
- 与单阀版**实现同一个 `I_Cylinder`** —— 里氏替换原则(LSP)：上层拿 `I_Cylinder` 编程，
  换单阀/双阀实现无需改动（见 `spt-cylinder-feedback` README 的多态示例）。
- 两个输出**互锁**：置一个必清另一个。