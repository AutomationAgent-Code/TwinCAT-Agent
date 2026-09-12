# PLC Coding Standard (自动遵循)

编写 / 修改 TwinCAT 3 ST 代码时**自动套用**的编码规范。完整规范见
[`docs/plc_coding_standard.md`](../../docs/plc_coding_standard.md)；本文件是**可执行摘要** ——
每次写 POU 前按此产出，写完按 §检查清单自查。

## Trigger
```
写/改任何 FB、POU、DUT、GVL 时自动应用（无需显式调用）
```

## 第 0 步：先查模板库，别急着手写

```bash
fblib find "要做的事"      # MCP: fblib_find(intent="我要写轴程序")
```
命中 → `fblib add <slug>` 落地后改。没命中时，在安装版 Agent 中，质量门禁启用的
标准事务型/循环服务型 FB 先 `plc_generate` 检查候选，再 `plc_create_standard_fb` 创建；
其他对象按项目规范生成。首次提交的候选就应满足规范，不先自由手写再依赖审核返工。
需要沉淀可复用实现时再用 `fblib extract`，不把每次改代码扩展为模板库维护。
`fblib list` 看全量分类（motion / framework / device / communication / tools）。

**两套风格互斥，选模板时看 `family` 字段：**

| family | 何时 | 命名风格 | lint |
|--------|------|---------|------|
| `spt-framework` | 项目用 SPT 库（`EXTENDS FB_PackML_BaseModule` / `FB_ComponentBase`） | SPT 式：只有指针/接口带前缀（`p`/`ip`），其余 PascalCase 全词 | `style: spt`（跳过命名与头注释检查） |
| `standalone` | 不依赖框架的独立 FB | 本文档的匈牙利式前缀 | 默认全套规则 |

同一个项目里两种可以共存（组件独立、模块接框架），但**同一个对象里别混**。

## 何时用哪种 FB

先分清**设备 FB 的两种原型**（判据：有没有"做完"这个概念）：
- **事务型**（`bExecute` 一次性触发）→ 完整状态四件套 `bDone/bBusy/bError/nErrId` + `CASE eState` 状态机（见下方骨架）。例：连接/收发/回零/定位。
- **循环服务型**（`bEnable` 常驻、每周期跑）→ 按需 `bError/nErrId`，**不需** `bDone/bBusy`，可无状态机。例：心跳、状态发布、数据映射、看门狗。

以及横切的：
- **OOP FB**：仅需继承/多态/接口复用（≥3 同族设备、PackML/SPT 框架）才用
  （`EXTENDS` + `IMPLEMENTS I_` + 方法 + `_` 内部变量）。拿不准就别上 OOP。

## 命名速查（英文标识符）

| 类别 | 前缀 | 例 |
|------|------|----|
| FB / 函数 / 程序 | `FB_` `F_` `PRG_` | `FB_Valve` |
| 接口/结构/枚举/联合 | `I_` `ST_` `E_` `U_` | `E_State` |
| 全局变量列表 | `GVL_` | `GVL_Io` |
| BOOL / 整型 / WORD | `b` `n` `w` | `bEnable` `nErrId` |
| REAL / STRING / TIME | `r`·`lr` `s` `t` | `lrPos` `tTimeout` |
| FB实例 / 枚举 / 结构 | `fb` `e` `st` | `fbTon` `eState` |
| 数组 / 指针 / 引用 / 接口 | `a` `p` `ref` `i` | `pData` |
| 常量 | 全大写无前缀 | `MAX_RETRY` |
| OOP 内部变量 | `_` + camelCase | `_retryCount` |

**禁止**：裸名 / 中文标识符 / 单字母（下标除外，推荐 `nIdx`）/ 魔法数字。

## 强制要素

1. **中文头注释块**（功能/作者/版本/日期/触发约定）在声明区首行。
2. **注释一律中文**，说"为什么"不复述"做什么"；物理量标单位 `// [mm/s]`。
3. **事务型** FB 必带**标准状态四件套**：`bDone` `bBusy` `bError` `nErrId`（互斥：Busy 与 Done/Error 不同真）。循环服务型只需按需 `bError/nErrId`。
4. **错误码集中** `GVL_ErrorCodes`（`ERR_*` 常量），禁魔法数字。
5. 状态机用 `CASE eState OF` + `E_*State`，禁散落布尔标志。
6. 缩进 **4 空格**、关键字大写、运算符两侧空格。
7. 枚举限定访问 `E_State.Idle`（配 `{attribute 'qualified_only'}`）。
8. 复位约定：`bExecute` 下降沿清 `bError`/`nErrId` 回 `Idle`。

## 设备型 FB 骨架（写新 FB 时直接套）

**声明区：**
```iecst
(*
 * 功能：<一句话职责>
 * 作者：<name> / 版本：1.0 / 日期：<yyyy-mm-dd>
 * 说明：<触发与复位约定>
 *)
FUNCTION_BLOCK FB_<Name>
VAR_INPUT
    bExecute : BOOL;   // 上升沿触发一次事务
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

**实现区：**
```iecst
CASE eState OF
    E_<Name>State.Idle:
        bBusy := FALSE;
        IF bExecute THEN
            bDone := FALSE; bError := FALSE;
            eState := E_<Name>State.Running;
        END_IF

    E_<Name>State.Running:
        bBusy := TRUE;
        // TODO: 业务逻辑；出错则 nErrId := ...; eState := ...Error
        eState := E_<Name>State.Done;

    E_<Name>State.Done:
        bBusy := FALSE; bDone := TRUE;
        IF NOT bExecute THEN eState := E_<Name>State.Idle; END_IF

    E_<Name>State.Error:
        bBusy := FALSE; bError := TRUE;
        IF NOT bExecute THEN eState := E_<Name>State.Idle; END_IF
END_CASE
```

> 用状态机 FB 时，记得同时建配套的 `E_<Name>State`（Idle/Running/Done/Error）枚举 DUT。

## 常见 IEC 陷阱（务必避开）

- 注释只有 `//` 和 `(* *)`，**没有** `#` 或 `/* */`。
- `TON`/`TOF` 必须每周期调用（`fbTon(IN:=..., PT:=...)`），不能只读 `.Q`。
- 边沿用 `R_TRIG`/`F_TRIG` 实例，别手写 `bOld` 比较。
- 计数器注意整型溢出与回绕。
- CiA 402 状态字/控制字按位判断，别整字比较。
- 赋值 `:=`，比较 `=`；别写反。

## 检查清单（写完自查 / `plc lint` 依据）

- [ ] 对象/变量前缀正确，无裸名·中文名·魔法数字
- [ ] FB 有中文头注释块
- [ ] 设备型 FB 有 `bDone/bBusy/bError/nErrId`
- [ ] 错误码来自 `GVL_ErrorCodes`
- [ ] 状态机用 `CASE eState`
- [ ] 4 空格缩进 / 关键字大写
- [ ] `plc build` **0 error**

## 参考

- 完整规范：`docs/plc_coding_standard.md`
- 写代码用 `/plc`（COM 读写），编译校验用 `plc build`
- 相关：`/plc`、`/twincat-library`、`anthropic-skills:iec-plc-programming-pitfalls`
