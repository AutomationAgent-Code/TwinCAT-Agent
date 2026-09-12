"""PLC 代码规范检查器 —— 对照 docs/plc_coding_standard.md §7 检查清单。

核心是纯函数 ``lint_objects(objects)``：吃 ``com_all_code()`` 的输出（每项
{name, folder, declaration, implementation, methods}），吐问题清单，不碰 COM，
可离线单测。CLI (`plc lint`) / MCP (`plc_lint`) 在其上包一层 COM 取码 + 可选
``plc build`` 0-error 校验。

规则（severity: error 仅编译；style 问题为 warning）:
  naming-object   对象名前缀不符 (FB_/F_/ST_/E_/U_/GVL_)
  fb-header       FB 缺中文头注释块 (* *)
  fb-status-quad  设备型 FB 缺标准状态四件套 bDone/bBusy/bError/nErrId
  naming-var      VAR 变量缺类型前缀 (b/n/lr/fb/e/...)
  magic-errid     nErrId := 非零字面量 (应引用 GVL_ErrorCodes 的 ERR_ 常量)
  tab-indent      使用了 Tab 缩进 (规范要求 4 空格)
  declaration-order  VAR 区域顺序不符合 Input/Output/InOut/Internal/Constant
  transaction-state  事务型 FB 缺 CASE eState 状态机或撤销触发复位路径
  transaction-timeout 事务型 FB 缺显式超时参数/定时器
  fb-call-count   子 FB 实例未调用或在一个周期路径中出现多次调用
"""

from __future__ import annotations

import re

# 变量已带前缀: 可选 _ (OOP 内部) + 1~4 小写字母 + 大写/数字。
# 例: bEnable nErrId lrPos fbTon eState 均通过; counter temp Motor x 不通过。
_PREFIXED = re.compile(r'^_?[a-z]{1,4}[A-Z0-9_]')
_ALL_CAPS = re.compile(r'^[A-Z][A-Z0-9_]*$')      # 常量允许全大写无前缀
_LOOP_OK = {'i', 'j', 'k'}                         # 循环下标豁免
# 行业约定名豁免: PLCopen/Beckhoff 的 MC 功能块统一用 `Axis : AXIS_REF` 作 VAR_IN_OUT,
# 官方例程与库全是这个写法; 改成 stAxis 反而不利于与官方文档对照。
_CONVENTIONAL = {'Axis'}
_ERRID_MAGIC = re.compile(r'\bnErrId\s*:=\s*(\d+)') # nErrId := 数字字面量
_DECLARATION_LINE = re.compile(
    r'^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*'
    r'(?:AT\s+%[^:]+)?\s*:\s*([^;]+?)\s*'
    r'(?::=.*)?;\s*(?://\s*(.*))?$', re.I,
)
_RANGED_TYPES = {
    'SINT', 'USINT', 'INT', 'UINT', 'DINT', 'UDINT', 'LINT', 'ULINT',
    'REAL', 'LREAL', 'TIME', 'LTIME',
}
_RANGE_HINT = re.compile(
    r'(?:\b(?:range|范围)\b|\d\s*(?:\.\.|~|至|to)\s*\d|\[[^\]]*\.\.[^\]]*\])', re.I,
)

# Only structural rules are promoted to blockers. Coding conventions remain
# visible advisories; missing documentation is not a source correctness error.
_WRITE_BLOCKING_RULES = {
    'interface-terminator',
    'interface-member-inline',
}


def _finding(rule: str, severity: str, obj: str, message: str,
             area: str = "") -> dict:
    return {"rule": rule, "severity": severity, "object": obj,
            "area": area, "message": message}


def _st_code(text):
    """Mask strings and nested IEC comments without moving source positions."""
    out, i, depth = list(text), 0, 0
    quote = ''
    while i < len(text):
        pair = text[i:i + 2]
        if depth:
            if pair == '(*': depth += 1
            elif pair == '*)': depth -= 1
            width = 2 if pair in {'(*', '*)'} else 1
        elif quote:
            width = 2 if text[i] == '$' else 1
            if text[i] == quote: quote = ''
        elif pair == '(*':
            depth, width = 1, 2
        elif pair == '//':
            end = text.find('\n', i)
            width = (len(text) if end < 0 else end) - i
        elif text[i] in {'\"', "'"}:
            quote, width = text[i], 1
        else:
            i += 1
            continue
        for n in range(i, min(i + width, len(text))):
            if text[n] not in '\r\n': out[n] = ' '
        i += width
    return ''.join(out)


def _conditional_call(code, variable):
    """Detect conditional/loop call sites regardless of state variable naming."""
    stack = []
    for token in re.finditer(r'\b[A-Za-z_]\w*\b', code):
        word = token.group().upper()
        if word in {'IF', 'CASE', 'FOR', 'WHILE', 'REPEAT'}:
            stack.append(word)
        elif word in {'END_IF', 'END_CASE', 'END_FOR', 'END_WHILE', 'END_REPEAT'}:
            if stack: stack.pop()
        elif token.group().casefold() == variable.casefold() and re.match(r'\s*\(', code[token.end():]):
            if stack: return True
    return False


def _fb_kind(decl: str) -> str:
    """判定声明区所属类型: fb / oop_fb / function / program / struct / enum / union / other."""
    u = decl.upper()
    if "FUNCTION_BLOCK" in u:
        # EXTENDS 或 IMPLEMENTS → OOP FB (跳过状态四件套/变量前缀检查)
        if re.search(r'\bEXTENDS\b|\bIMPLEMENTS\b', u):
            return "oop_fb"
        return "fb"
    if "FUNCTION " in u or u.strip().startswith("FUNCTION"):
        if "FUNCTION_BLOCK" not in u:
            return "function"
    if "PROGRAM" in u:
        return "program"
    if re.search(r'\bSTRUCT\b', u):
        return "struct"
    if re.search(r'\bUNION\b', u):
        return "union"
    if re.search(r'TYPE\s+\w+\s*:\s*\(', decl):
        return "enum"
    return "other"


_OBJ_PREFIX = {
    "fb": "FB_", "oop_fb": "FB_", "function": "F_",
    "struct": "ST_", "enum": "E_", "union": "U_",
}


def lint_objects(objects: list[dict], style: str = "default") -> list[dict]:
    """对 com_all_code() 输出逐项检查，返回问题清单 (warning 级 style 问题)。

    ``style='spt'``: 按 Beckhoff SPT Application Framework 的风格指南放宽 ——
    SPT 只给指针/接口加前缀 (p/ip)，模块与组件用 PascalCase 全词命名
    (``Machine`` / ``Infeed`` / ``VAxis``)，不带 FB_ 前缀、不写中文头注释块。
    对这类代码套本项目的匈牙利式规则只会刷屏，故跳过命名与头注释三条。
    """
    from .plc import _parse_variables  # 复用纯文本 VAR 解析器

    spt = (style or "default").lower() == "spt"
    findings: list[dict] = []

    for o in objects:
        name = o.get("name", "") or ""
        folder = o.get("folder", "") or ""
        decl = o.get("declaration", "") or ""
        impl = o.get("implementation", "") or ""
        kind = _fb_kind(decl)
        method_text = "\n".join(
            str(m.get("implementation") or m.get("code") or "")
            for m in (o.get("methods") or []) if isinstance(m, dict)
        )
        executable = _st_code(impl + "\n" + method_text)

        # ── naming-object: 对象名前缀 ──
        want = None if spt else _OBJ_PREFIX.get(kind)
        if want and not name.startswith(want):
            findings.append(_finding(
                "naming-object", "warning", name,
                f"{kind} 命名应以 '{want}' 开头"))
        if not spt and folder == "GVLs" and not name.startswith("GVL_"):
            findings.append(_finding(
                "naming-object", "warning", name, "GVL 命名应以 'GVL_' 开头"))
        if (not spt and kind == "program" and name.upper() != "MAIN"
                and not name.startswith("PRG_")):
            findings.append(_finding(
                "naming-object", "warning", name, "程序命名应以 'PRG_' 开头 (或 MAIN)"))

        # ── tab-indent ──
        for area, txt in (("declaration", decl), ("implementation", impl)):
            if "\t" in txt:
                findings.append(_finding(
                    "tab-indent", "warning", name, "使用了 Tab 缩进，应改为 4 空格", area))

        # TwinCAT stores an interface header only in the declaration editor.
        # Its methods/properties live as separate child objects, so a complete
        # ST-block terminator is invalid in this particular text field.
        if folder == "Interfaces" and re.search(r'^\s*END_INTERFACE\b', decl, re.I | re.M):
            findings.append(_finding(
                "interface-terminator", "warning", name,
                "接口声明区只保留 'INTERFACE <名称>'；方法/属性作为子节点，禁止 END_INTERFACE",
                "declaration"))
        if folder == "Interfaces" and re.search(
            r'^\s*(?:METHOD|PROPERTY)\b', decl, re.I | re.M
        ):
            findings.append(_finding(
                "interface-member-inline", "warning", name,
                "接口方法/属性必须使用 plc_create_member 创建为接口子对象，禁止写进接口声明文本",
                "declaration"))

        # 设备型 FB 专项检查
        if kind == "fb":
            # ── fb-header: 头注释块 (接受 (* *) 块 或 >=2 行 // 块) ──
            before = decl.split("FUNCTION_BLOCK")[0]
            has_block = "(*" in before
            has_line_block = sum(
                1 for ln in before.splitlines() if ln.strip().startswith("//")) >= 2
            if not (has_block or has_line_block) and not spt:
                findings.append(_finding(
                    "fb-header", "warning", name,
                    "FB 缺中文头注释块 (* 功能/作者/版本/日期 *)", "declaration"))

            # ── fb-status-quad: 仅事务型 FB (有 bExecute) 才要求完整四件套 ──
            #   循环服务型 FB (bEnable 常驻, 无 bExecute) 不强制 bDone/bBusy。
            var_names = {v["name"] for v in _parse_variables(decl)}
            if "bExecute" in var_names:
                missing = [q for q in ("bDone", "bBusy", "bError", "nErrId")
                           if q not in var_names]
                if missing:
                    findings.append(_finding(
                        "fb-status-quad", "warning", name,
                        f"事务型 FB 缺标准状态输出: {', '.join(missing)}", "declaration"))
                if not re.search(r'\bCASE\s+eState\s+OF\b', impl, re.I):
                    findings.append(_finding(
                        "transaction-state", "warning", name,
                        "事务型 FB 应使用 CASE eState OF 状态机", "implementation"))
                if not re.search(r'\bNOT\s+bExecute\b', impl, re.I):
                    findings.append(_finding(
                        "transaction-state", "warning", name,
                        "事务型 FB 缺少 bExecute 撤销后的复位路径", "implementation"))
                has_timeout = (
                    "tTimeout" in var_names
                    or any(v["type"].upper().startswith(("TON", "TOF", "TP"))
                           for v in _parse_variables(decl))
                )
                if not has_timeout:
                    findings.append(_finding(
                        "transaction-timeout", "warning", name,
                        "事务型 FB 缺显式超时参数或定时器", "declaration"))

            # 声明区顺序：外部接口在前，内部实现和常量在后。
            section_rank = {
                "VAR_INPUT": 0, "VAR_OUTPUT": 1, "VAR_IN_OUT": 2,
                "VAR": 3, "VAR CONSTANT": 4,
            }
            seen_sections = []
            for line in decl.splitlines():
                upper = line.strip().upper()
                if upper in section_rank:
                    seen_sections.append(upper)
            ranks = [section_rank[s] for s in seen_sections]
            if ranks != sorted(ranks):
                findings.append(_finding(
                    "declaration-order", "warning", name,
                    "声明区应按 VAR_INPUT → VAR_OUTPUT → VAR_IN_OUT → VAR → VAR CONSTANT 排列",
                    "declaration"))

            # FB/定时器实例必须有明确周期调用；重复文本调用通常表示同周期重复执行。
            # OOP/command FB 可能由多个显式方法调度，单靠文本无法判定它们是否同周期执行，所以只报告主体循环 FB。
            if impl.strip() and not method_text:
                for variable in _parse_variables(decl):
                    vn = variable["name"]
                    vt = variable["type"].upper().split()[0]
                    if not (vt.startswith("FB_") or vt in {"TON", "TOF", "TP", "R_TRIG", "F_TRIG"}):
                        continue
                    count = len(re.findall(rf'\b{re.escape(vn)}\s*\(', executable))
                    if count != 1:
                        findings.append(_finding(
                            "fb-call-count", "warning", f"{name}.{vn}",
                            f"子 FB 实例 '{vn}' 应在周期调用区调用一次，当前文本调用次数为 {count}",
                            "implementation"))
                # A child block must be called before the state machine. The
                # state machine may prepare its inputs, but must not contain a
                # conditional call site that skips a PLC cycle.
                    if _conditional_call(_st_code(impl), vn):
                        findings.append(_finding(
                            'fb-call-zone', 'warning', f'{name}.{vn}',
                            f"子 FB 实例 '{vn}' 位于条件/循环分支，应在独立调用区每周期无条件调用一次",
                            'implementation'))

        if kind == 'program':
            for variable in _parse_variables(decl):
                vn, vt = variable['name'], variable['type'].upper().split()[0]
                if (vt.startswith('FB_') or vt in {'TON','TOF','TP','R_TRIG','F_TRIG'}) and _conditional_call(_st_code(impl), vn):
                    findings.append(_finding('fb-call-zone', 'warning', f'{name}.{vn}',
                        f"子 FB 实例 '{vn}' 位于条件/循环分支，应每周期无条件调用一次", 'implementation'))

        # Declaration hygiene. Every declared variable needs a useful
        # right-side comment; externally supplied numeric/time values must
        # additionally state their permitted range. Internal counters and
        # status outputs are intentionally not forced to carry a range.
        scope = ''
        for raw_line in decl.splitlines():
            upper = raw_line.strip().upper()
            if upper.startswith('END_VAR'):
                scope = ''
                continue
            if upper.split() and upper.split()[0] in {
                'VAR_INPUT', 'VAR_OUTPUT', 'VAR_IN_OUT', 'VAR',
            }:
                scope = upper.split()[0]
                continue
            match = _DECLARATION_LINE.match(raw_line)
            if not match or not scope:
                continue
            names, var_type, comment = match.groups()
            for var_name in (n.strip() for n in names.split(',')):
                label = f'{name}.{var_name}'
                if not (comment or '').strip():
                    findings.append(_finding(
                        'declaration-comment', 'warning', label,
                        f"变量 '{var_name}' 缺少右侧 // 注释（用途/单位/默认值）",
                        'declaration'))
                base_type = var_type.upper().split()[0]
                if (scope in {'VAR_INPUT', 'VAR_IN_OUT'}
                        and kind == 'fb'
                        and base_type in _RANGED_TYPES
                        and not _RANGE_HINT.search(comment or '')):
                    findings.append(_finding(
                        'declaration-range', 'warning', label,
                        f"外部参数 '{var_name}' ({base_type}) 的注释缺少允许范围",
                        'declaration'))

        # ── naming-var: 变量前缀 (跳过 DUT 成员 与 OOP FB) ──
        if folder != "DUTs" and kind != "oop_fb" and not spt:
            for v in _parse_variables(decl):
                vn = v["name"]
                if vn in _LOOP_OK or vn in _CONVENTIONAL:
                    continue
                if _ALL_CAPS.match(vn):        # 常量豁免
                    continue
                if not _PREFIXED.match(vn):
                    findings.append(_finding(
                        "naming-var", "warning", f"{name}.{vn}",
                        f"变量 '{vn}' 缺类型前缀 (b/n/lr/s/fb/e/st/...)", "declaration"))

        # ── magic-errid: nErrId := 数字 ──
        for m in _ERRID_MAGIC.finditer(impl):
            if m.group(1) != "0":              # := 0 (清零) 允许
                findings.append(_finding(
                    "magic-errid", "warning", name,
                    f"nErrId := {m.group(1)} 用了魔法数字，应引用 GVL_ErrorCodes 的 ERR_ 常量",
                    "implementation"))

    # error 在前，其次按对象名
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["object"]))
    return findings


def review_write_candidate(candidate: dict, changed_area: str = 'all',
                           style: str = 'default') -> dict:
    """Review the *post-write* PLC object and classify gate findings.

    Syntax/structure and error-level correctness findings block writes.
    Documentation, naming, layout and framework conventions are advisories.
    ``changed_area`` scopes the findings to the edited region.
    """
    area = (changed_area or 'all').lower()
    from .conditional_compilation import preprocess_candidate
    candidate, preprocessing = preprocess_candidate(candidate)
    # All pre-write analyzers must see the same selected branches. Unknown
    # branches are not scanned as if simultaneously active.
    candidate = dict(candidate)
    preprocessing_findings = []
    for key, result in preprocessing.items():
        if result['status'] != 'processed':
            candidate[key] = ''
            preprocessing_findings.append({'rule':'semantic-unresolved','severity':'error',
                'object':candidate.get('name',''),'area':'implementation' if key=='implementation' else 'declaration',
                'line':1,'message':result['reason']})
    findings = lint_objects([candidate], style=style)
    findings.extend(preprocessing_findings)
    from .plc_syntax import syntax_findings
    findings.extend(syntax_findings(candidate))
    # Safety-level TCSA checks are deliberately part of the pre-write review.
    # They remain a separate command/report for project-wide analysis, but a
    # candidate with a direct input write or literal division by zero must not
    # be silently accepted just because it satisfies naming/style rules.
    from .static_analysis import analyze_objects
    static_result = analyze_objects([candidate], project_wide=False)
    findings.extend(static_result["findings"])
    if area == 'declaration':
        relevant = [f for f in findings if f.get('area') == 'declaration']
    elif area == 'implementation':
        relevant = [
            f for f in findings
            if f.get('area') == 'implementation'
            or f.get('rule') == 'fb-status-quad'
        ]
    else:
        relevant = findings
    blocking_rules = set(_WRITE_BLOCKING_RULES)
    blocking = [
        f for f in relevant
        if f.get('rule') in blocking_rules or f.get('severity') == 'error'
    ]
    return {
        'approved': not blocking,
        'changed_area': area,
        'summary': summarize(findings),
        'blocking_findings': blocking,
        'advisories': [f for f in relevant if f not in blocking],
    }


def summarize(findings: list[dict]) -> dict:
    """按规则聚合计数。"""
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return {"total": len(findings), "by_rule": by_rule}
