# FB_CylinderDualOutputWithFeedback（SPT SOLID 组件）

带到位反馈的双阀气缸。**来源**：[SPT Libraries · Cylinder · Dual Output with Sensor Feedback](https://beckhoff-usa-community.github.io/SPT-Libraries/SPT%20Training/Components/Examples/CylinderDualOutputWithFeedback.html)

## 依赖模板
`requires: [spt-cylinder-dual, spt-digital-input]` —— 递归带入全部 4 个下层组件。

## 用法
```iecst
VAR
    bExtValve  AT %Q* : BOOL;   bRetValve  AT %Q* : BOOL;
    bExtSensor AT %I* : BOOL;   bRetSensor AT %I* : BOOL;

    fbExtOut : FB_DigitalOutput(Output := bExtValve);
    fbRetOut : FB_DigitalOutput(Output := bRetValve);
    fbExtSns : FB_DigitalInput(Input := bExtSensor);
    fbRetSns : FB_DigitalInput(Input := bRetSensor);

    fbCyl : FB_CylinderDualOutputWithFeedback(
                ExtendOutput := fbExtOut, RetractOutput := fbRetOut,
                ExtendSensor := fbExtSns, RetractSensor := fbRetSns);
END_VAR

fbCyl.Extend();
IF fbCyl.IsExtended THEN ... END_IF
```

## 多态示例（LSP）
同一段控制代码，运行期切换具体实现：
```iecst
VAR
    iCyl : I_Cylinder;
END_VAR

IF bUseSpringCylinder THEN
    iCyl := fbSpringCyl;      // FB_CylinderSingleOutput
ELSE
    iCyl := fbCyl;            // FB_CylinderDualOutputWithFeedback
END_IF
iCyl.Extend();                // 控制代码无需知道是哪种气缸
```

## 设计要点
- `I_CylinderWithFeedback **EXTENDS** I_Cylinder`：用扩展接口加反馈，而不是往
  `I_Cylinder` 里塞属性 —— 后者会迫使所有既有实现都得实现反馈，
  破坏开闭原则(OCP)与接口隔离原则(ISP)。
- 四路依赖全部注入抽象接口，各配空对象兜底。

## 与官方文档的一处差异
官方示例里反馈属性写作 `ExtendSensor.On`，但 `I_DigitalInput` 只有 `IsActive`
（文档笔误）。本模板用 `IsActive`，否则编译不过。