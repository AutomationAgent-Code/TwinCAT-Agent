"""XAE split-editor syntax examples and conservative lexical checks.

Examples are not a substitute for compilation on the selected compiler version.
"""
import re
from copy import deepcopy

SOURCES = [
    'https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84182539.html',
    'https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84179467.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529316491.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529332619.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/5044757003.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/8825253771.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/8825257611.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529410443.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2529437323.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/25282000754359667083.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/11982693643.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528880779.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528875403.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/3998090635.html',
    'https://infosys.beckhoff.com/content/1033/tc3_automationinterface/242732427.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/4256428299.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/4256479627.html',
    'https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528286347.html',
]

# A compact, versioned contract is injected before generation, not merely
# returned after a rejected mutation. Keep vendor rules distinct from style.
GENERATION_RULES = [
    '注释/命名/编码模板仅建议，不阻断写入；语法结构、明确错误及审批/并发保护不变。',
    '内建调用与库函数分开：TIME()无参数返回TIME；SIZEOF接受变量或类型名。XSIZEOF要求4026，结果保留__UXINT，不猜目标位宽。MEMSET/MEMCPY/MEMMOVE/MEMCMP要求实际Tc2_System引用，使用官方PVOID/UDINT签名；签名通过不证明地址、长度、重叠或在线内存安全。',
    'Tc2_TcpIp 的 T_HSOCKET 是结构体，不是整数：禁止 hSocket=0、hSocket:=0 或猜测 bInit。公开成员为 handle:UDINT、localAddr/remoteAddr:ST_SockAddr；后者含 nPort:UDINT、sAddr:STRING(15)。按官方接口使用成员，连接有效性仍结合 FB 完成/错误状态，不以句柄数值证明在线。',
    '名称按局部/所属 FB 与基类/全局/项目类型/库解析；同名或 qualified_only 时使用实际限定名，不能全局猜第一个匹配项。',
    '区分 Region 编辑器折叠与影响编译的属性/条件 Pragma；后者必须保留并核对配置，不能剥除后宣称语义通过。',
    'LDATE/LDT/LTOD 及长日期别名要求 TwinCAT 3.1.4026.0 或以上；核对实际编译器版本，不从安装包名字或候选代码猜版本。',
    '整块结构体通信不能简单累加成员字节数；核对目标位宽、TwinCAT 3 对齐、pack_mode 和实际布局证据，FB 有隐式成员。',
    '条件编译宏按声明/实现编辑器分别生效；未知工程或系统变体宏不等于未定义。先确认生效分支，禁止把未生效分支的缺少符号当作代码错误。',
    '非字符串常量传入 VAR_IN_OUT CONSTANT 前核对当前项目 ReplaceConstants；候选代码自报选项不是证据。版本需求未核实和 memory_layout 计算均不代表实际编译或运行验证。',
    '固定数组上下界为有序 DINT 范围整数；变长 ARRAY[*] 在 VAR_IN_OUT 声明。不要使用 BIT 数组、POINTER TO BIT 或 REFERENCE TO BIT。',
    'FB_init/FB_exit/FB_reinit 保持官方 BOOL 接口和必需输入，不增加输出；FB_init 内不能调用 SUPER^.FB_init，基类初始化由系统执行。',
    '变量标识符用英文字母/数字/下划线且不以数字开头；同一声明作用域不区分大小写，不能重复；普通 CONSTANT 必须初始化，VAR_IN_OUT CONSTANT 不写初始化值。',
    '可写 VAR_IN_OUT 字符串实参容量不得小于形参；只读字符串参数不套用这个限制。',
    'CONSTANT 只读：实现区不能赋值、作为 FOR 控制变量、输出接收变量或传给可写 VAR_IN_OUT；声明初始化不属于运行时写入。',
    'MOD 仅接受整数或位串类型；除法和 MOD 不使用零除数，动态除数需根据程序逻辑保护，不能把静态检查当运行时安全证明。',
    'VAR_IN_OUT 按引用传递，不能套用普通输入的数值隐式转换；VAR_IN_OUT CONSTANT 字符串允许不同长度的变量或字面量。',
    '多对象修改先调用 plc_preflight，提供完整候选和精确路径；阻断时不得写入。已降级语义提示不等于失败，保留未验证项；通过不是编译证明。',
    'XAE 声明/实现分离；签名和 VAR...END_VAR 放声明区，不写 END_PROGRAM/END_FUNCTION_BLOCK/END_FUNCTION/END_METHOD。',
    'DUT 保留 TYPE...END_TYPE、STRUCT/UNION 闭合；方法、属性和 Get/Set 独立创建，不嵌入父对象。',
    '转换用 SOURCE_TO_TARGET(x)/TO_TARGET(x)，如 UDINT_TO_DINT(nErrId)。禁止 DINT(value)、TYPE#(expression)。TIME() 可为库函数而非强转；核对时间单位、目标范围。',
    '先读实际枚举声明；赋值、条件、CASE 均用 qualified existing members，不猜 Running/Starting/Done；整数状态须显式映射。',
    '先读实际 GVL/FB/成员签名及库；精确到 PLC 项目。依赖缺失、歧义或截断时不猜读。',
    '闭合 IF/CASE/FOR/WHILE/REPEAT、括号、字符串和嵌套注释；赋值 :=，比较 =，语句用分号。',
    '回读只验证保存内容；XAE 完整编译才可 compiler_verified=true；按错误文件/行号修复，缺失诊断禁止猜改。',
]


def generation_contract():
    return {'version': 3, 'dialect': 'TwinCAT ST / XAE split editor',
            'sources': list(SOURCES),
            'rules': list(GENERATION_RULES), 'compiler_verified': False,
            'scope': 'Deterministic preconditions, not a complete compiler or library type checker.'}
_TEMPLATES = {
    'fb': ('FUNCTION_BLOCK FB_Example\nVAR_INPUT\n    bEnable : BOOL; // 循环使能\nEND_VAR\nVAR_OUTPUT\n    bActive : BOOL; // 活动状态\nEND_VAR', 'bActive := bEnable;'),
    'program': ('PROGRAM PRG_Example\nVAR\n    bReady : BOOL; // 就绪状态\nEND_VAR', 'bReady := TRUE;'),
    'function': ('FUNCTION F_Example : BOOL\nVAR_INPUT\n    bValue : BOOL; // 输入状态\nEND_VAR', 'F_Example := bValue;'),
    'struct': ('TYPE ST_Example :\nSTRUCT\n    bReady : BOOL; // 就绪状态\nEND_STRUCT\nEND_TYPE', ''),
    'enum': ("{attribute 'qualified_only'}\n{attribute 'strict'}\nTYPE E_Example :\n(\n    Idle := 0,\n    Running := 1\n) DINT;\nEND_TYPE", ''),
    'union': ('TYPE U_Example :\nUNION\n    nValue : DINT; // 整数视图\n    dwValue : DWORD; // 位视图\nEND_UNION\nEND_TYPE', ''),
    'gvl': ('VAR_GLOBAL\n    bReady : BOOL; // 就绪状态\nEND_VAR', ''),
    'method': ('METHOD PUBLIC IsReady : BOOL\nVAR_INPUT\n    bValue : BOOL; // 输入状态\nEND_VAR', 'IsReady := bValue;'),
    'interface': ('INTERFACE I_Example', ''),
    'interface_method': ('METHOD IsReady : BOOL', ''),
    'property': ('PROPERTY PUBLIC Ready : BOOL', ''),
    'interface_property': ('PROPERTY Ready : BOOL', ''),
    'get': ('', 'Ready := TRUE;'),
    'set': ('', '// Ready 是传入的属性值；在此转存到所属 FB 的已声明变量。'),
    'interface_get': ('', ''),
    'interface_set': ('', ''),
}


def syntax_templates(kind=''):
    if kind and kind not in _TEMPLATES:
        raise ValueError('Unknown syntax kind: ' + kind)
    return deepcopy({'status': 'reference', 'compiler_verified': False,
        'editor_model': 'XAE separate DeclarationText / ImplementationText; not PLCopen full-source text',
        'templates': {k: {'declaration': d, 'implementation': i} for k, (d, i) in _TEMPLATES.items() if not kind or k == kind},
        'generation_contract': generation_contract(),
        'rules': [
            'Never append END_PROGRAM/END_FUNCTION_BLOCK/END_FUNCTION/END_METHOD/END_INTERFACE in the split editor.',
            'DUT retains END_TYPE and END_STRUCT/END_UNION. VAR blocks retain END_VAR.',
            'Methods, properties and Get/Set are separate child objects, not inline declarations in the parent.',
            'Interface methods/properties/accessors carry signatures only, no implementation.',
            'Get/Set examples belong under a BOOL property Ready; do not insert GET/SET headers.',
            'FB example is only a syntax reference; use fblib_find and plc_generate for application FBs.',
        ], 'sources': SOURCES})


def syntax_findings(candidate):
    from .lint import _st_code
    from .conditional_compilation import preprocess_candidate
    candidate,_=preprocess_candidate(candidate)
    findings = []
    for area in ('declaration', 'implementation'):
        raw = candidate.get(area) or ''
        from .conditional_compilation import preprocess_editor
        raw = preprocess_editor(raw,area)['source']
        code = _st_code(raw)
        def add(rule, position, message):
            findings.append({'rule': rule, 'severity': 'error', 'object': candidate.get('name', ''),
                             'area': area, 'line': code.count('\n', 0, position) + 1, 'message': message})
        # Diagnose unterminated literals/comments before masking their bodies.
        i, depth, quote, opened = 0, 0, '', 0
        while i < len(raw):
            pair = raw[i:i + 2]
            if depth:
                if pair == '(*':
                    depth += 1
                elif pair == '*)':
                    depth -= 1
                i += 2 if pair in {'(*', '*)'} else 1
            elif quote:
                if raw[i] == '$':
                    i += 2
                else:
                    if raw[i] == quote:
                        quote = ''
                    i += 1
            elif pair == '//':
                end = raw.find('\n', i)
                i = len(raw) if end < 0 else end + 1
            elif pair == '(*':
                opened, depth, i = i, 1, i + 2
            elif raw[i] in {'"', "'"}:
                opened, quote, i = i, raw[i], i + 1
            else:
                i += 1
        if depth or quote:
            add('syntax-unclosed-literal', opened,
                'Unclosed comment' if depth else 'Unclosed STRING/WSTRING literal')
        for match in re.finditer(r'\bEND_(?:PROGRAM|FUNCTION_BLOCK|FUNCTION|METHOD|INTERFACE|PROPERTY)\b', code, re.I):
            add('syntax-editor-terminator', match.start(), 'XAE split editor does not use this object terminator; keep declaration and implementation separate.')
        # Conditional-compilation branches may be unbalanced individually.
        # Do not falsely reject their aggregate; compiler preprocessing is authoritative.
        if re.search(r'\{\s*(?:IF|ELSIF|ELSE|END_IF)\b', code, re.I):
            continue
        code = re.sub(r'\{[^}]*\}', lambda m: re.sub(r'[^\r\n]', ' ', m[0]), code)
        if area == 'declaration':
            from .type_contract import enum_definition
            enum = enum_definition(code)
            if enum and enum['status']=='error':
                add('syntax-enum-contract',0,enum['reason'])
            from .declaration_contract import declaration_findings
            for rule, position, message in declaration_findings(code):
                add(rule, position, message)
            from .lifecycle_contract import lifecycle_findings
            for rule, position, message in lifecycle_findings(code):
                add(rule, position, message)
        elif re.search(r'\bMETHOD\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL|ABSTRACT)\s+)*FB_INIT\b', candidate.get('declaration', ''), re.I):
            for match in re.finditer(r'\bSUPER\s*\^\s*\.\s*FB_INIT\s*\(', code, re.I):
                add('syntax-lifecycle-super-init', match.start(), 'Do not call SUPER^.FB_init: base initialization is implicit.')
        for match in re.finditer(r'\b(?:SINT|USINT|INT|UINT|DINT|UDINT|LINT|ULINT|REAL|LREAL|BOOL|BYTE|WORD|DWORD|LWORD)\s*\(', code, re.I):
            # INT(0..100) is a valid declaration of a subrange, not a cast.
            if area == 'declaration' and not re.search(r':=[^;]*$', code[:match.start()]):
                continue
            add('syntax-invalid-conversion', match.start(),
                'A type name is not a conversion function. Use SOURCE_TO_TARGET(value) or TO_TARGET(value).')
        pairs = {'IF': 'END_IF', 'CASE': 'END_CASE', 'FOR': 'END_FOR',
                 'WHILE': 'END_WHILE', 'REPEAT': 'END_REPEAT'} if area == 'implementation' else {
                     'VAR': 'END_VAR', 'VAR_INPUT': 'END_VAR', 'VAR_OUTPUT': 'END_VAR',
                     'VAR_IN_OUT': 'END_VAR', 'VAR_GLOBAL': 'END_VAR', 'VAR_TEMP': 'END_VAR',
                     'VAR_EXTERNAL': 'END_VAR', 'VAR_STAT': 'END_VAR', 'VAR_INST': 'END_VAR',
                     'STRUCT': 'END_STRUCT', 'UNION': 'END_UNION', 'TYPE': 'END_TYPE'}
        stack = []
        for match in re.finditer(r'\b[A-Za-z_]\w*\b', code):
            token = match[0].upper()
            if token in pairs:
                stack.append((pairs[token], match.start()))
            elif token in pairs.values():
                if not stack or stack[-1][0] != token:
                    add('syntax-block-pair', match.start(), 'Unexpected block terminator ' + token)
                else:
                    stack.pop()
        for expected, position in stack:
            add('syntax-block-pair', position, 'Missing block terminator ' + expected)
        stack = []
        for match in re.finditer(r'[()\[\]]', code):
            token = match[0]
            if token in '([':
                stack.append((token, match.start()))
            elif not stack or stack[-1][0] != {')': '(', ']': '['}[token]:
                add('syntax-delimiter', match.start(), 'Unmatched delimiter ' + token)
            else:
                stack.pop()
        for token, position in stack:
            add('syntax-delimiter', position, 'Unclosed delimiter ' + token)
    return findings


PROMPT = ('语法优先：生成对象前查询 plc_syntax_templates，声明/实现及成员对象分开。'
          '类型转换必须 SOURCE_TO_TARGET(x) 或 TO_TARGET(x)，禁止 DINT(x) 等类型名强转；'
          '先读取实际枚举和依赖声明再生成，CASE/条件/赋值不得猜测成员名。'
          '候选先通过 plc_preflight/写前审核，未知语义不放行；不要把当前工程作为编译试错区。'
          '写后回读只确认保存；原生编译是另一个验证层，不替代前置审核。'
          'diagnostics_complete=false 时停止代码猜改，先用 plc_diagnostics 只读恢复诊断；重复同一诊断且无进展时停止并报告。'
          '一次修正一组根因，最多三轮自动修正，超限报告剩余错误；不得关闭检查或改平台/库/编码来掩盖错误。'
          '编译失败不授权登录、下载、激活、启动；质量警告与编译错误分别报告。')
