# FB_CylinderSingleOutput（SPT SOLID 组件）

单电磁阀气缸（弹簧复位）。**来源**：[SPT Libraries · Cylinder · Single Output](https://beckhoff-usa-community.github.io/SPT-Libraries/SPT%20Training/Components/Examples/CylinderSingleOutput.html)

## 依赖模板
`requires: [spt-digital-output]` —— `fblib add` 会自动一并塞入。

## 用法
```iecst
VAR
    bValveVar AT %Q* : BOOL;
    fbValve   : FB_DigitalOutput(Output := bValveVar);
    fbCyl     : FB_CylinderSingleOutput(ExtendOutput := fbValve);
END_VAR

fbCyl.Extend();    // 得电伸出
fbCyl.Retract();   // 失电，弹簧复位
```

## 设计要点
- 依赖 **`I_DigitalOutput` 抽象**而非具体 FB —— 依赖倒置原则(DIP)，便于替换/测试。
- **空对象(Null Object)**：未注入输出时挂内置兜底实例，调用不会碰空引用。
- `I_Cylinder` 故意保持最小（只有 Extend/Retract）；带反馈的用扩展接口，见 `spt-cylinder-feedback`。