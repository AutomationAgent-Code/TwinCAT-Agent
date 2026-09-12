"""标准 FB 脚手架 —— 生成符合 docs/plc_coding_standard.md 的设备型状态机 FB。

纯字符串模板，不碰 COM。CLI (`plc create-fb`)、MCP (`plc_create_fb`) 复用本模块，
保证脚手架输出与规范文档 / plc-coding-standard skill 完全一致。

产出：
  * 设备型 FB 骨架（中文头注释 + 标准状态四件套 + CASE 状态机）
  * 配套 E_<Name>State 枚举（qualified_only，Idle/Running/Done/Error）
"""

from __future__ import annotations

from datetime import date


def normalize_fb_name(name: str) -> str:
    """确保 FB 名带 FB_ 前缀（规范 §1.1）。"""
    name = name.strip()
    return name if name.startswith("FB_") else f"FB_{name}"


def _base_name(fb_name: str) -> str:
    """去掉 FB_ 前缀，取基础名（用于派生枚举名）。"""
    return fb_name[3:] if fb_name.startswith("FB_") else fb_name


def state_enum(fb_name: str) -> dict:
    """生成配套状态枚举 E_<Base>State 的声明。"""
    base = _base_name(normalize_fb_name(fb_name))
    ename = f"E_{base}State"
    decl = (
        "{attribute 'qualified_only'}\n"
        "{attribute 'strict'}\n"
        f"TYPE {ename} :\n"
        "(\n"
        "    Idle := 0,   // 空转，等待触发\n"
        "    Running,     // 执行中\n"
        "    Done,        // 完成\n"
        "    Error        // 出错\n"
        ") DINT;\n"
        "END_TYPE"
    )
    return {"enum_name": ename, "enum_declaration": decl}


def standard_fb(fb_name: str, author: str = "", purpose: str = "") -> dict:
    """生成标准设备型 FB 的声明区 + 实现区 + 配套枚举。

    Args:
        fb_name: FB 名（缺 FB_ 前缀会自动补）。
        author: 作者，填入头注释。
        purpose: 一句话职责，填入头注释。

    Returns:
        {fb_name, fb_declaration, fb_implementation, enum_name, enum_declaration}
    """
    fb_name = normalize_fb_name(fb_name)
    e = state_enum(fb_name)
    ename = e["enum_name"]
    today = date.today().isoformat()
    purpose = purpose or "<一句话职责>"
    author = author or "<name>"

    decl = f"""(*
 * 功能：{purpose}
 * 作者：{author} / 版本：1.0 / 日期：{today}
 * 说明：bExecute 上升沿触发一次事务；bBusy 期间禁止再次触发
 *)
FUNCTION_BLOCK {fb_name}
VAR_INPUT
    // 控制输入
    bExecute : BOOL;          // 上升沿触发一次事务
    tTimeout : TIME := T#5S;  // 事务超时，允许范围 [T#100MS..T#60S]
END_VAR
VAR_OUTPUT
    // 标准状态输出
    bDone    : BOOL;   // 完成
    bBusy    : BOOL;   // 执行中
    bError   : BOOL;   // 出错
    nErrId   : UDINT;  // 错误码 → GVL_ErrorCodes
END_VAR
VAR
    // 内部状态与子功能块实例
    eState   : {ename};  // 状态机
    fbTimeout : TON;     // 事务超时监控，每周期调用一次
END_VAR
VAR CONSTANT
    // 本地默认错误码；集成项目时应映射到 GVL_ErrorCodes
    ERR_TIMEOUT_FB : UDINT := 16#8001;
END_VAR"""

    impl = f"""// 子功能块调用区：先设置输入，每个实例每周期调用一次，再由状态机读取输出
fbTimeout(
    IN := bBusy,
    PT := tTimeout);

// 主状态机
CASE eState OF
    {ename}.Idle:
        bBusy := FALSE;
        IF bExecute THEN
            bDone := FALSE; bError := FALSE;
            eState := {ename}.Running;
        END_IF

    {ename}.Running:
        bBusy := TRUE;
        IF fbTimeout.Q THEN
            nErrId := ERR_TIMEOUT_FB;
            eState := {ename}.Error;
        ELSE
            // TODO: 业务逻辑；成功后进入 Done，失败后设置错误码并进入 Error
            eState := {ename}.Done;
        END_IF

    {ename}.Done:
        bBusy := FALSE;
        bDone := TRUE;
        IF NOT bExecute THEN eState := {ename}.Idle; END_IF

    {ename}.Error:
        bBusy := FALSE;
        bError := TRUE;
        IF NOT bExecute THEN
            bError := FALSE;
            nErrId := 0;
            eState := {ename}.Idle;
        END_IF
END_CASE"""

    return {
        "fb_name": fb_name,
        "fb_declaration": decl,
        "fb_implementation": impl,
        **e,
    }


def service_fb(fb_name: str, author: str = "", purpose: str = "") -> dict:
    """Generate a continuously running service FB without transaction outputs."""
    fb_name = normalize_fb_name(fb_name)
    today = date.today().isoformat()
    purpose = purpose or "<一句话职责>"
    author = author or "<name>"
    declaration = f"""(*
 * 功能：{purpose}
 * 作者：{author} / 版本：1.0 / 日期：{today}
 * 说明：bEnable 为 TRUE 时每周期执行；禁用时输出复位
 *)
FUNCTION_BLOCK {fb_name}
VAR_INPUT
    // 控制输入
    bEnable : BOOL;  // 循环服务使能，允许值 [FALSE..TRUE]
END_VAR
VAR_OUTPUT
    // 服务状态输出
    bError : BOOL;   // 服务异常状态
    nErrId : UDINT;  // 错误码 → GVL_ErrorCodes
END_VAR
VAR
    // 内部状态与子功能块实例
    bActive : BOOL;  // 当前服务已启用
END_VAR
VAR CONSTANT
    // 本地无错误常量；集成项目时应映射到 GVL_ErrorCodes
    ERR_NONE : UDINT := 0;
END_VAR"""
    implementation = """// 周期服务逻辑
IF NOT bEnable THEN
    bActive := FALSE;
    bError := FALSE;
    nErrId := ERR_NONE;
    RETURN;
END_IF

bActive := TRUE;

// TODO: 每周期业务逻辑；子 FB 应在本段之前设置输入并且无条件调用一次
"""
    return {
        "fb_name": fb_name,
        "fb_declaration": declaration,
        "fb_implementation": implementation,
        "enum_name": "",
        "enum_declaration": "",
    }


def generate_fb(fb_name: str, author: str = "", purpose: str = "",
                mode: str = "transaction") -> dict:
    """Select the standard transaction or continuous-service skeleton."""
    selected = str(mode or "transaction").casefold()
    if selected == "transaction":
        result = standard_fb(fb_name, author=author, purpose=purpose)
    elif selected == "service":
        result = service_fb(fb_name, author=author, purpose=purpose)
    else:
        raise ValueError("mode must be 'transaction' or 'service'")
    result["mode"] = selected
    return result
