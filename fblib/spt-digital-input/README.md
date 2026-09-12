# FB_DigitalInput（SPT SOLID 组件）

把一个硬件 BOOL 输入包装成对象。**来源**：[SPT Libraries · SOLID Components · Digital Input](https://beckhoff-usa-community.github.io/SPT-Libraries/SPT%20Training/Components/Examples/DigitalInput.html)

## 用法
```iecst
VAR
    bSensorVar AT %I* : BOOL;
    fbSensor   : FB_DigitalInput(Input := bSensorVar);
END_VAR

IF fbSensor.IsActive THEN ... END_IF
```

## 设计要点
- IsActive **只读**（无 Set）—— 输入不该被程序改写，符合接口隔离原则(ISP)。
- 与 FB_DigitalOutput 同样用 FB_Init 注入硬件引用。