# TwinCAT 3 MC3 (TF55xx) — 知识索引

来源：`source/TF55xx_TC3_MC3_EN.pdf`

- 文档：Beckhoff《TwinCAT 3 | MC3》
- 产品族：TF5500 / TF551x / TF553x / TF5550 / TF5560
- 版本：1.0.3
- 日期：2026-08-20
- 语言：English
- 页数：353
- SHA-256：`543513A9838ECFD0F7D68FC61220856B835931D637F5018EDEEF26D6AADB85A5`

> 本文件是供 Agent 检索的结构化索引。参数定义、限制条件和安全相关内容以原始 PDF 为准。

---

## 1. MC3 定位与运行要求

TwinCAT 3 MC3 是新一代 TwinCAT Motion Control。核心特点：

- 可将轴计算分配到多个 CPU 核，并支持跨核同步运动。
- 同一 CPU 核可运行不同周期的轴任务。
- 使用 TcCOM 对象和接口实现模块化扩展。
- 轴对象隔离具体硬件，支持仿真轴、Beckhoff 轴和第三方轴。
- 电轴与液压轴可在统一架构下使用。

安装与版本要求（手册第 8-9 页）：

- 工程侧安装 MC3；运行侧要求 TwinCAT Standard 3.1 Build 4026.23.1 或更高版本。
- 激活配置时，工程系统会将需要的 MC3 组件传输到运行系统。
- 基础工程工作负载：`TF5500.MC3.Base.XAE`。
- 电子凸轮扩展：`TF5550.MC3.Camming.XAE`。
- 液压扩展：`TF5560.MC3.FluidPower.XAE`。
- 推荐配套：Drive Manager 2、TwinCAT Scope。

---

## 2. 许可模型

- `TF5500`：MC3 Base，是所有 MC3 轴应用的前置许可；包含 10 个轴许可，其中最多 5 个可用于第三方轴。
- `TF5510` 至 `TF5518`：将总轴数扩展到 25、50、75、100、150、250、500、1000 或 1500。
- `TF5530` 至 `TF5535`：第三方轴 Multi Vendor 扩展，档位为 25、50、75、100、150、250。
- `TF5550`：Camming，非线性耦合和电子凸轮。
- `TF5560`：Fluid Power，液压运动功能。

注意：第三方轴同时需要轴许可和 Multi Vendor 许可；TF551x 的轴数量是新的总上限，不是在 Base 的 10 轴上累加。

---

## 3. XAE 工程组态索引

### 3.1 SYSTEM（第 12-14 页）

- `SYSTEM > Real-Time > Settings`：CPU 核配置和任务分配。
- `SYSTEM > Tasks`：周期任务设置。
- `SYSTEM > TcCOM Objects`：MC3 运行对象和扩展对象。

### 3.2 MOTION（第 15-190 页）

- `MC Project`（第 17 页）：创建 MC 工程。
- `General`（第 18 页）：工程通用设置。
- `Context`（第 19-21 页）：创建 MC Context，并将轴分配到 Context。
- `Axes`（第 22 页）：轴对象集合。
- `Default Axis`（第 25-131 页）：标准运动轴及其参数、数据区和调试 UI。
- `Encoder Axis`（第 132-134 页）：编码器轴。
- `FluidPower Axis`（第 135-184 页）：液压轴。
- `Planar Mover`、`Groups`、`Tables`（第 185-188 页）。
- `Objects`、`Extensions`（第 189-190 页）。

Default Axis 主要页签：

- `Object`
- `Settings`
- `Init Parameter`
- `Online Parameter`
- `Data Area`
- `Motion UI`

工具实现时应把 MC Project、Context、Axis、Task 作为不同层级对象处理，不应沿用旧 NC2 中“固定 NC Task + Axis”的单一假设。

---

## 4. 全局类型与轴引用

全局类型位于手册第 193-218 页。

### 4.1 重要枚举

- `EAxisLicenseSetting`
- `EAxisState`
- `EContinuity`
- `EControlErrorSetpointMode`
- `EControlValueType`
- `ECylinderDesign`
- `EDeadTimeCompensationMode`
- `EEncoderDataRange`
- `EEncoderFeedbackVeloSource`
- `EEncoderInterpretation`
- `EFilterType`
- `EHomingObjectType`
- `EHomingSyncSource`
- `ELoadType`
- `EMcTaskInternalSequence`
- `EPositionLagSource`
- `EPositionUnit`
- `ETouchProbeTriggerEdge`
- `ETouchProbeTriggerSource`
- `ETouchProbeTriggerState`

### 4.2 PLC/MC 数据交换结构

- `CDT_MCTOPLC_AXIS_ACT`
- `CDT_MCTOPLC_AXIS_FLUIDPOWER`
- `CDT_MCTOPLC_AXIS_LOADCONTROL`
- `CDT_MCTOPLC_AXIS_MODULO`
- `CDT_MCTOPLC_AXIS_SET`
- `CDT_MCTOPLC_AXIS_STD`
- `CDT_MCTOPLC_CAMTABLE`
- `CDT_MCTOPLC_ENCODER_AXIS_ACT`
- `CDT_MCTOPLC_ENCODER_AXIS_MODULO`
- `CDT_MCTOPLC_ENCODER_AXIS_STD`
- `CDT_MCTOPLC_VALVECHARACTERISTICCURVE`
- `CDT_PLCTOMC_AXIS_STD`

### 4.3 轴引用与能力接口

- `AXIS_REF`：支持常规 PTP 运动的轴引用。
- `ENCODER_AXIS_REF`：编码器轴引用，不支持所有 PTP 命令；例如不能直接用于 `MC_MoveAbsolute`。
- MC3 通过接口类型表达对象能力，如 `Type_Master1D`、`Type_Camming1D`、`Type_Phasing1D`、`Type_PtpTouchProbe`。
- 外部设定值接口：`ITcMcExternalSetpointGenerator`。

工具在生成 PLC 声明或连接对象时必须校验“引用类型是否具备目标功能”，不能只按变量名或对象路径判断。

---

## 5. PLC 库

### 5.1 `Tc3_Mc3Base`（第 220-228 页）

核心类型和接口：

- `EBufferMode`
- `EReactionToMasterError`
- `EReactionToSlaveError`
- `ESyncDirection`
- `Type_Camming1D`
- `Type_ErrorTriggerable`
- `Type_Master1D`
- `Type_MotionObject`
- `Type_Phasing1D`

功能块：`MC_TriggerError`。

特殊数值常量包括 `IGNORE`、`INVALID`、`INFINITY`、`INFINITY_NEGATIVE`。MC3 使用标准 `LREAL`，不再依赖旧式 `MC_LREAL` 类型。

### 5.2 `Tc3_Mc3Ptp`（第 229-308 页）

#### 管理与状态

- `MC_Power`
- `MC_ReadActualPosition`
- `MC_ReadActualTorque`
- `MC_ReadActualVelocity`
- `MC_ReadAxisError`
- `MC_ReadStatus`
- `MC_Reset`
- `MC_SetOverride`
- `MC_SetPosition`

#### 参数读写

- `MC_ReadBoolParameter`
- `MC_ReadHardwareParameter`
- `MC_ReadParameter`
- `MC_WriteBoolParameter`
- `MC_WriteHardwareParameter`
- `MC_WriteParameter`

#### 连续与离散运动

- `MC_Jog`
- `MC_MoveVelocity`
- `MC_Halt`
- `MC_MoveAbsolute`
- `MC_MoveModulo`
- `MC_MoveRelative`
- `MC_Stop`
- `MC_WaitForStableActualPosition`

`MC_Stop` 会锁定轴，必须等轴静止且 `Execute := FALSE` 后才释放；`MC_Halt` 停止后不保持这种运动命令锁定。

#### 回零、负载控制与同步

- `MC_Home`
- `MC_SetHomingState`
- `MC_LoadControl`
- `MC_GearIn`
- `MC_GearInPos`
- `MC_HaltPhasing`
- `MC_PhasingAbsolute`
- `MC_PhasingRelative`

#### Touch Probe

- `BinaryTouchProbeRef`
- `DriveTouchProbeRef`
- `EncoderTouchProbeRef`
- `FixedStopTouchProbeRef`
- `TimestampTouchProbeRef`
- `MC_TouchProbe`
- `MC_AbortTrigger`

#### 外部设定值

- `MC_ActivateExternalSetpointGeneration`
- `MC_ExternalSetpointGenerator`
- `ITcMcExternalSetpointGenerator`

自定义外部设定值功能块需要实现接口及其回调，并遵守手册中的 `FB_init`、`FB_reinit` 和 online change 要求，详见第 278-282、302-303 页。

### 5.3 `Tc3_Mc3Camming`（第 309-340 页）

主要功能块与对象：

- `MC_CamScaling`
- `MC_CamTableSelect`
- `MC_CamAdd`
- `MC_CamExchange`
- `MC_CamIn`
- `MC_CamRemove`
- `MC_CamObject`

`MC_CamObject` 是凸轮表的中心对象，支持连接、创建、读写、覆盖、复制、清空和特征计算。主要方法：

- `Connect`
- `Create`
- `WriteData`
- `ReadData`
- `OverwriteData`
- `ReadSlaveDynamics`
- `ComputeMasterPositions`
- `ReadMasterRange`
- `ComputeCharacteristics`
- `ClearData`
- `CopyFrom`

### 5.4 `Tc3_Mc3FluidPower`（第 341-348 页）

- `ValveCharacteristicPoint`
- `ValveCharacterizationOptions`
- `MC_ValveCharacteristicCurve`
- `MC_ValveCharacterization`

`MC_ValveCharacteristicCurve` 提供 `Connect`、`ReadData`、`OverwriteData`、`ClearData`、`CopyFrom`、`Enable`、`Disable`、`ComputeVelocityValue` 和 `ComputeSpoolPositionValue`。

---

## 6. NC2 迁移到 MC3 时的重点

迁移说明位于第 299-308 页。开发工具和模板不可假定 MC3 与 `Tc2_MC2` 完全兼容：

- 一些旧功能块被新的功能块、接口或命令取消机制替代。
- `MC_Power` 的输入、override 行为和错误状态处理存在差异。
- `MC_MoveVelocity` 的速度方向语义与 NC2 不同，MC3 使用带符号速度。
- `MC_Stop` 与 `MC_Halt` 的锁定语义需要明确区分。
- 轴错误复位后，应用通常需要重新执行期望的耦合命令。
- 外部设定值生成改为接口驱动的 MC Context 回调模型。
- 凸轮操作集中到 `MC_CamObject`。

生成 MC3 PLC 示例时必须引用 `Tc3_Mc3Base` / `Tc3_Mc3Ptp` / 相应扩展库，不应直接复用 `Tc2_MC2` 模板。

---

## 7. 面向本仓库工具开发的检索入口

- 创建 MC Project / Context / Axis：第 15-27 页。
- Axis 参数、数据区和 Motion UI：第 27-131 页。
- Encoder Axis：第 132-134 页。
- FluidPower Axis：第 135-184 页。
- 全局数据类型与能力接口：第 193-218 页。
- PTP 状态机和 PLC 基本示例：第 229-233 页。
- PTP 数据类型与功能块：第 234-298 页。
- NC2 到 MC3 迁移：第 299-308 页。
- Camming：第 309-340 页。
- Fluid Power PLC API：第 341-348 页。
- 示例程序入口：第 349-350 页。
