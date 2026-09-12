# TwinCAT 3 PLC 编码规范

> 2026-09-12 写入策略调整：下文的注释、命名、单位/范围备注、声明排序、状态四件套、
> 状态机/超时模板和子 FB 调用区属于生成建议，不再作为写入硬门禁。
> 用户明确要求优先；不要仅为满足这些建议反复修改代码或中止任务。
> 语法/对象结构和明确正确性错误仍阻断；语义证据、审批、并发编辑保护及写后验证不变。

> 版本：1.0 · 生效日期：2026-07-26
> 适用：本仓库及本仓库 Agent 生成的所有 TwinCAT 3 结构化文本（ST）代码
> 风格基调：**混合风格** —— 匈牙利-lite 命名 + 状态机为主、OOP 为辅；
> **英文标识符 + 中文注释**。

本规范是"规范化四件套"的**基准文档**，其余三件（`plc-coding-standard` skill、
标准 FB 脚手架、`plc lint` 检查器）都以本文为准。

---

## 0. 总则

1. **标识符一律英文**（变量、FB、方法、类型名）—— TwinCAT 关键字与库均为英文，中文标识符易踩编码坑且不兼容库。
2. **注释一律中文** —— 说明"为什么"，而非复述代码"做什么"。
3. **一个 POU 一个职责**；FB 越小越可测。
4. **编译零容忍**：提交前 `plc build` 必须 **0 error**；warning 尽量清零并记录例外原因。
5. 新代码默认走**设备型状态机 FB**；仅当需要继承/多态复用时才用 OOP FB（见 §5）。

---

## 1. 命名约定

### 1.1 对象（POU / 类型）前缀

| 前缀 | 对象 | 示例 |
|------|------|------|
| `FB_` | 功能块 Function Block | `FB_ModbusTcpClient` |
| `F_`  | 函数 Function | `F_ScaleAnalogInput` |
| `PRG_`（或 `MAIN`） | 程序 Program | `PRG_Main`、`PRG_Safety` |
| `I_`  | 接口 Interface | `I_Axis` |
| `ST_` | 结构 Struct（DUT） | `ST_AxisStatus` |
| `E_`  | 枚举 Enum（DUT） | `E_MachineState` |
| `U_`  | 联合 Union（DUT） | `U_RawData` |
| `GVL_` | 全局变量列表 | `GVL_Io`、`GVL_ErrorCodes` |
| `ITF_`/`PROP_` | 一般不单独前缀，属性用 PascalCase | `Position` |

> 类型名用 **PascalCase**（前缀后首字母大写）：`FB_HomeRoutine`、`ST_AxisStatus`。

### 1.2 变量前缀（按类型）

| 前缀 | 类型 | 示例 |
|------|------|------|
| `b`  | BOOL | `bEnable`、`bError` |
| `n`  | 整型 (SINT/INT/DINT/LINT 及无符号) | `nCounter`、`nErrId` |
| `w`  | WORD / DWORD / 位串 | `wStatus`、`dwControl`（DWORD 可用 `dw`） |
| `r` / `lr` | REAL / LREAL | `rSetpoint`、`lrPosition` |
| `s`  | STRING / WSTRING | `sName`、`sErrMsg` |
| `t`  | TIME / LTIME | `tTimeout` |
| `dt` | DATE_AND_TIME / DT | `dtStamp` |
| `fb` | FB 实例 | `fbConnect`、`fbTonDelay` |
| `e`  | 枚举变量 | `eState` |
| `st` | 结构变量 | `stStatus` |
| `a`  | 数组 | `aBuffer`（配合元素类型：`anValues`） |
| `p`  | 指针 POINTER TO | `pData` |
| `ref`| 引用 REFERENCE TO | `refAxis` |
| `i`  | 接口变量 | `iAxis` |

### 1.3 作用域 / 特殊

- **常量**（VAR CONSTANT / VAR_GLOBAL CONSTANT）：全大写下划线，无类型前缀 —— `MAX_RETRY`、`DEFAULT_TIMEOUT`。
- **OOP 内部变量**（仅 OOP FB 用）：`_` 前缀 + camelCase —— `_extending`、`_retryCount`。
- **I/O 映射变量**：`AT %I*` / `AT %Q*`，命名带方向语义 —— `bSensorHome AT %I*`、`bValveOpen AT %Q*`。
- **全局变量**：放 `GVL_*`，引用时写全 `GVL_Io.bEStop`，禁止裸全局名。

### 1.4 禁止

- ❌ 无前缀的裸变量名（`counter`、`temp`、`x`）
- ❌ 中文/拼音标识符
- ❌ 单字母变量（循环下标 `i`/`j`/`k` 例外，但推荐 `nIdx`）
- ❌ 复用变量做不同用途

---

## 2. 声明区（Declaration）规范

### 2.1 FB 头注释块（必填）

每个 FB / 函数声明区**第一行开始**是中文头注释块：

```iecst
(*
 * 功能：Modbus TCP 客户端，封装 socket 连接/收发/断开的完整生命周期
 * 作者：Ethan
 * 版本：1.0
 * 日期：2026-07-26
 * 说明：bExecute 上升沿触发一次完整事务；bBusy 期间禁止再次触发
 *)
FUNCTION_BLOCK FB_ModbusTcpClient
```

### 2.1b 两类设备 FB 原型（先分清是哪种）

| 原型 | 触发 | 状态输出 | 实现 | 例 |
|------|------|---------|------|----|
| **事务型** | `bExecute` 上升沿，一次性 | 完整四件套 `bDone/bBusy/bError/nErrId` | `CASE eState` 状态机 | 连接/收发/定位/回零 |
| **循环服务型** | `bEnable` 常驻，每周期跑 | 按需 `bError/nErrId`（可失败时）；**不需** `bDone/bBusy` | 直接周期逻辑，可无状态机 | 心跳、状态发布、数据映射、看门狗 |

> 判据：**有没有"做完"这个概念**。有一次性完成语义 → 事务型；一直跑没有终点 → 循环服务型。
> `plc lint` 据此区分：仅当 FB 有 `bExecute` 才检查完整四件套。

### 2.2 声明区分区顺序（事务型 FB）

严格按此顺序，每区一行中文小标题：

```iecst
FUNCTION_BLOCK FB_Example
VAR_INPUT
    // 控制输入
    bExecute    : BOOL;          // 上升沿触发一次事务
    nUnitId     : BYTE;          // Modbus 从站地址
END_VAR
VAR_OUTPUT
    // 标准状态四件套
    bDone       : BOOL;          // 事务成功完成（一个周期脉冲/保持至复位）
    bBusy       : BOOL;          // 正在执行
    bError      : BOOL;          // 出错
    nErrId      : UDINT;         // 错误码，见 GVL_ErrorCodes
END_VAR
VAR_IN_OUT
    // 引用型双向参数（可选）
END_VAR
VAR
    // 内部状态与子实例
    eState      : E_ClientState;  // 状态机当前状态
    fbConnect   : FB_SocketConnect;
    fbSend      : FB_SocketSend;
    nRetry      : UINT;
END_VAR
VAR CONSTANT
    MAX_RETRY   : UINT := 3;
END_VAR
```

### 2.3 声明卫生

- 变量**必须带类型注释**（右侧 `//`），说明用途/单位/取值范围。
- 需要初值的给初值：`tTimeout : TIME := T#5S;`。
- 物理量注释单位：`lrSpeed : LREAL;  // 速度 [mm/s]`。
- 相关变量用空行分组，组前一行中文小标题。

---

## 3. 实现区（Implementation）规范

### 3.0 声明区与调用区边界（强制）

- 声明区只放接口、变量、实例和常量，顺序固定为：`VAR_INPUT`、`VAR_OUTPUT`、
  `VAR_IN_OUT`、`VAR`、`VAR CONSTANT`。
- 每个区先写中文分组标题；变量右侧注明职责、单位、允许范围和默认值。
- 实现区开头设置独立的“子功能块调用区”。调用顺序统一为：先准备输入，随后每个实例
  每周期无条件调用一次，最后在状态机或输出整理区读取结果。
- 禁止把 `TON`、通信 FB、PLCopen MC FB 等需要连续周期调用的实例放入可能跳过的
  `IF/CASE` 分支；命令是否执行应通过实例的 `Execute/Enable` 输入控制。
- 同一实例不得在一个 PLC 周期内从多个位置重复调用。若不同状态需要不同参数，应先在
  状态机中计算命令变量，再在唯一调用点调用实例。

推荐结构：

```iecst
// 子功能块调用区：先写输入，再唯一调用
fbTimeout(
    IN := bBusy,
    PT := tTimeout);

fbCommand(
    bExecute := bStartCommand,
    tTimeout := tCommandTimeout);

// 主状态机：只读取子 FB 输出并决定状态迁移
CASE eState OF
    // ...
END_CASE

// 输出整理区
bError := fbCommand.bError;
nErrId := fbCommand.nErrId;
```

### 3.1 缩进与格式

- 缩进 **4 空格**，不用 Tab。
- 关键字大写：`IF / THEN / CASE / FOR / WHILE / RETURN`。
- 运算符两侧空格：`nSum := nA + nB;`。
- 一行一语句；`END_IF` / `END_CASE` 单独成行。

### 3.2 注释

- 每个逻辑段前一行中文注释说明**意图**。
- 复杂条件旁注中文解释。
- 禁止注释掉的死代码进版本库（用版本控制历史）。

### 3.3 设备型 FB 的状态机范式（主推）

用 `CASE eState OF` + `E_State` 枚举，禁止散落的布尔标志堆砌：

```iecst
// 子 FB 调用区：先设置输入，每周期无条件调用一次；状态分支只读取输出。
fbConnect.bExecute := (eState = E_ClientState.Connecting) AND bExecute;
fbConnect();

// 空转：等待触发
CASE eState OF
    E_ClientState.Idle:
        bBusy := FALSE;
        IF bExecute THEN
            bDone  := FALSE;
            bError := FALSE;
            nRetry := 0;
            eState := E_ClientState.Connecting;
        END_IF

    E_ClientState.Connecting:
        bBusy := TRUE;
        IF fbConnect.bDone THEN
            eState := E_ClientState.Sending;
        ELSIF fbConnect.bError THEN
            nErrId := fbConnect.nErrId;
            eState := E_ClientState.Error;
        END_IF

    E_ClientState.Error:
        bBusy  := FALSE;
        bError := TRUE;
        IF NOT bExecute THEN            // 复位：撤触发后回空转
            eState := E_ClientState.Idle;
        END_IF
END_CASE
```

### 3.4 定时器 / 边沿

- 定时器实例化：`fbTimeout : TON;`，命名体现用途。
- 边沿检测用 `R_TRIG` / `F_TRIG` 实例，禁止手写 `bOld` 布尔比较（除非性能敏感且注释）。

---

## 4. 错误处理契约

统一状态四件套：`bDone` / `bBusy` / `bError` / `nErrId`。

1. **互斥**：`bBusy` 与 `bDone`/`bError` 不同时为 TRUE。
2. **错误码集中**：所有 `nErrId` 取值定义在 `GVL_ErrorCodes`（或 `E_Error` 枚举），禁止魔法数字。
   ```iecst
   VAR_GLOBAL CONSTANT
       ERR_NONE          : UDINT := 0;      // 无错误
       ERR_CONNECT_FAIL  : UDINT := 16#8001; // 连接失败
       ERR_TIMEOUT       : UDINT := 16#8002; // 超时
   END_VAR
   ```
3. **可选** `sErrMsg : STRING` 给 HMI 显示，但 `nErrId` 是权威。
4. **复位约定**：`bExecute`/`bEnable` 撤销（下降沿）时清 `bError`/`nErrId` 回 Idle。

---

## 5. OOP FB 规范（仅复杂可复用模块）

当 FB 需要**继承 / 多态 / 接口复用**（如设备组件族、PackML 模块）才用 OOP。参考
`reference/PackML_PLC_Example` 的 `FB_Cylinder` 风格：

```iecst
FUNCTION_BLOCK FB_Cylinder EXTENDS FB_ComponentBase IMPLEMENTS I_Cylinder
VAR
    Output    AT %Q* : BOOL;      // 电磁阀输出
    _extending       : BOOL;      // OOP 内部变量用 _ 前缀
    _extendTime      : LREAL := 1000; // 默认伸出时间 [ms]
END_VAR
```

- 循环逻辑放 `METHOD CyclicLogic`，由基类统一调度。
- 方法可见性显式声明：`METHOD PROTECTED CreateEvents`、`METHOD PUBLIC Reset`。
- 属性（Property）用 PascalCase：`Position`、`IsExtended`。
- 接口 `I_*` 只声明契约，命名对应 FB 能力。
- 用 `SUPER^.Method()` 调基类实现。

> 何时用 OOP：≥3 个同族设备共享行为、需要运行期多态、或对接现成 OOP 框架（PackML/SPT）。
> 否则一律用 §3 的状态机 FB —— 更简单、更易读、门槛低。

---

## 6. 项目组织

- POU 按功能分文件夹：`POUs/`、`POUs/Function Blocks/`、`POUs/Modules/`。
- 类型（DUT）集中在 `DUTs/`，全局变量在 `GVLs/`。
- 一个功能块一个文件（TwinCAT 默认）。
- I/O 链接变量集中在 `GVL_Io`，PLC 逻辑不直接摸物理地址。

---

## 7. 检查清单（提交前 / `plc lint` 依据）

- [ ] 所有对象名带正确前缀（§1.1）
- [ ] 所有变量名带类型前缀（§1.2），无裸名/中文名
- [ ] 每个 FB 有中文头注释块（§2.1）
- [ ] 设备型 FB 有标准状态四件套 `bDone/bBusy/bError/nErrId`（§2.2 / §4）
- [ ] 错误码来自 `GVL_ErrorCodes`，无魔法数字（§4）
- [ ] 状态机用 `CASE eState`，无散落布尔标志（§3.3）
- [ ] 缩进 4 空格，关键字大写（§3.1）
- [ ] `plc build` **0 error**（§0.4）

---

## 附录 A · 快速模板（设备型 FB 骨架）

```iecst
(*
 * 功能：<一句话职责>
 * 作者：<name> / 版本：1.0 / 日期：<yyyy-mm-dd>
 * 说明：<触发与复位约定>
 *)
FUNCTION_BLOCK FB_<Name>
VAR_INPUT
    bExecute : BOOL;   // 上升沿触发
END_VAR
VAR_OUTPUT
    bDone    : BOOL;   // 完成
    bBusy    : BOOL;   // 执行中
    bError   : BOOL;   // 出错
    nErrId   : UDINT;  // 错误码 → GVL_ErrorCodes
END_VAR
VAR
    eState   : E_<Name>State;   // 状态机
END_VAR
```

```iecst
// 实现区骨架
CASE eState OF
    E_<Name>State.Idle:
        bBusy := FALSE;
        IF bExecute THEN
            bDone := FALSE; bError := FALSE;
            eState := E_<Name>State.Running;
        END_IF

    E_<Name>State.Running:
        bBusy := TRUE;
        // TODO: 业务逻辑
        eState := E_<Name>State.Done;

    E_<Name>State.Done:
        bBusy := FALSE; bDone := TRUE;
        IF NOT bExecute THEN eState := E_<Name>State.Idle; END_IF

    E_<Name>State.Error:
        bBusy := FALSE; bError := TRUE;
        IF NOT bExecute THEN eState := E_<Name>State.Idle; END_IF
END_CASE
```
