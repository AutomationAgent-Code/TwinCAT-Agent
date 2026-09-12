# FB_DigitalOutput（SPT SOLID 组件）

把一个硬件 BOOL 输出包装成对象。**来源**：[SPT Libraries · SOLID Components · Digital Output](https://beckhoff-usa-community.github.io/SPT-Libraries/SPT%20Training/Components/Examples/DigitalOutput.html)

## 依赖
无（纯 IEC，不需要 SPT 库）

## 用法
```iecst
VAR
    bValveVar AT %Q* : BOOL;                        // 硬件变量必须【先】声明
    fbValve   : FB_DigitalOutput(Output := bValveVar);  // FB_Init 注入
END_VAR

fbValve.On := TRUE;    // 得电
fbValve.On := FALSE;   // 失电
```

## 设计要点
- **依赖注入**：硬件变量经 FB_Init 以 REFERENCE TO BOOL 注入，组件不写死具体地址。
- **只经属性访问**：不用 VAR_INPUT/VAR_OUTPUT，符合 SPT 组件规范。
- **FB 本体无代码**：SPT 规定组件逻辑只能在方法/属性里。

## 阀岛（Solenoid Bank）用法
供应商常把多个电磁阀合并成一个 WORD 输出。按位拆成 BOOL 再各自注入：
```iecst
VAR
    wBankVar AT %Q* : WORD;
    bSol1 : BOOL;  bSol2 : BOOL;
    fbSol1 : FB_DigitalOutput(Output := bSol1);
    fbSol2 : FB_DigitalOutput(Output := bSol2);
END_VAR

fbSol1.On := TRUE;
// 末尾把各位写回字
wBankVar.0 := bSol1;
wBankVar.1 := bSol2;
```