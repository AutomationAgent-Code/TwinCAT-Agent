# FB_AxisControl（OOP）

单轴控制器（Tc2_MC2）—— `I_AxisControl` 接口 + `FB_AxisControl` 实现。把
`MC_Power` / `MC_MoveAbsolute` / `MC_Home` / `MC_Stop` / `MC_Reset` 封装成
"命令方法 + 只读属性"的对象接口。

## 依赖
- 库：`Tc2_MC2`
- `Init()` 传入的 `AXIS_REF` 需链接到已在 System Manager 配置好的 NC 轴

## 用法三步
```iecst
PROGRAM MAIN
VAR
    axisX   : AXIS_REF;          // 链接到 NC 轴
    fbAxisX : FB_AxisControl;
    bInit   : BOOL := TRUE;
END_VAR

// 1) 上电绑定一次
IF bInit THEN
    bInit := NOT fbAxisX.Init(axisX);
END_IF

// 2) 每周期驱动
fbAxisX.Cyclic();

// 3) 下命令 / 读状态
IF fbAxisX.Ready AND NOT fbAxisX.Busy THEN
    fbAxisX.MoveAbsolute(250.0, 300.0);   // 位置, 速度(<=0 用默认)
END_IF
```

## 接口 I_AxisControl
| 类别 | 成员 | 说明 |
|------|------|------|
| 方法 | `Cyclic()` | **每周期必须调用**，驱动状态机与 MC 功能块 |
| 方法 | `MoveAbsolute(lrPosition, lrVelocity)` | 绝对定位；仅在 Ready 且空闲时受理，返回是否受理 |
| 方法 | `Home()` | 回零；同上 |
| 方法 | `Stop()` | 停止当前动作 |
| 方法 | `Reset()` | 清除轴错误 |
| 属性 | `Enabled` (读写) | 轴使能开关（默认 TRUE） |
| 属性 | `Ready` | 轴已就绪（MC_Power.Status） |
| 属性 | `Busy` | 有动作在执行 |
| 属性 | `Done` | 上一个动作成功完成（保持到下一次命令） |
| 属性 | `Error` / `ErrorId` | 错误标志 / 错误码 |
| 属性 | `ActPosition` | 实际位置（未 Init 时返回 0） |
| 属性 | `State` | `E_AxisState`，便于 HMI 诊断 |

> `FB_AxisControl` 比接口多一个 `Init(refAxis)` —— 绑定轴属于实现细节，不进契约。

## 参数
| 参数 | 默认 | 说明 |
|------|------|------|
| `DEFAULT_VELOCITY` | `100.0` | `MoveAbsolute` 传入速度 <=0 时使用 |
| `DEFAULT_OVERRIDE` | `100.0` | 速度倍率 [%] |
| `HOME_POSITION` | `DEFAULT_HOME_POSITION` | 回零后设定的绝对位置 |

## 塞入
```bash
fblib add axis-control -P DEFAULT_VELOCITY=200.0
```

## 设计要点
- **命令方法只登记请求**，真正执行在 `Cyclic()` 的状态机里 —— 避免在方法里
  直接驱动 MC 块导致时序错乱（MC 功能块必须每周期调用）。
- **动作块 Execute 复位**：不在对应状态时把 `Execute` 拉低，功能块才能接受下一次上升沿。
- **空闲时的 Stop 请求直接丢弃**，否则会残留到下一次动作把它立刻中止。
- `ActPosition` 读前校验 `__ISVALIDREF`，未 `Init` 时不会解引用无效引用。

## API 说明（踩过的坑）
以下均按 InfoSys 原文核准：
- `MC_Power` 的输入是 **`Enable` / `Enable_Positive` / `Enable_Negative` / `Override`**，
  没有 `bRegulatorOn` / `bEnablePositive` 这类名字。
- `MC_MoveVelocity` 的完成输出是 **`InVelocity`**，不是 `Done`。
- `MC_SetOverride` 用 **`VelFactor`**，没有 `Value` 输入。
- 各 MC 块的 `Axis` 是 **`VAR_IN_OUT`**；OOP 里用 `REFERENCE TO AXIS_REF` + `REF=` 绑定。
- 建 property 时 TwinCAT **不会**自动生成 Get/Set，须显式创建访问器节点。

## ⚠️ 验证状态
- ✅ 编译验证：塞入真实项目（含 Tc2_MC2）编译 **0 error**
- ❌ **运行时未验证**：状态机时序、实际轴运动行为尚未在真实/仿真轴上跑过。
  上机前请在安全环境（低速、限位就绪）先行验证。
