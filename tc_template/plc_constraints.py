"""Agent-authored PLC writing guidance; no vendor binaries or license dependency.

SA identifiers are documentation cross-references, not compatibility claims.
Keep the executable coverage explicit: semantic guidance is not a passed check.
"""
from copy import deepcopy

SOURCE = 'https://infosys.beckhoff.com/content/1033/te1200_tc3_plcstaticanalysis/3475825675.html'

# id, authoring requirement, independent checker (empty = author review only)
_RULES = (
    ('SA0001', '避免不可达代码；检查 RETURN 后语句及恒真/恒假分支。', ''),
    ('SA0004', '硬件输出集中在明确位置写入；审阅所有写入路径。', ''),
    ('SA0006', '跨任务共享变量明确唯一写入者或同步协议。', ''),
    ('SA0020', '整数运算转 REAL/LREAL 前检查溢出和精度；必要时先转换操作数。', ''),
    ('SA0026', '字符串拼接和赋值检查目标容量及截断处理。', ''),
    ('SA0027', '检查应用及引用库的标识符冲突；qualified_only 枚举豁免该规则，不能把 strict 当作同等豁免。', 'TCSA0027'),
    ('SA0028', 'AT 地址分配检查范围重叠，不能只检查变量名。', ''),
    ('SA0033', '检查编译代码未使用的变量，不限局部变量；外部接口保留项需另核对用途。', 'TCSA0033'),
    ('SA0037', '不要在实现中赋值给 VAR_INPUT；使用内部变量保存工作值。', 'TCSA0037'),
    ('SA0038', 'POU 内部不要读取自己的 VAR_OUTPUT 作为中间状态；内部状态用 VAR，最后赋给输出；包括方法内读取。', 'TCSA0038'),
    ('SA0039', '指针解引用前验证有效性和生命周期。', ''),
    ('SA0040', '除法/MOD 前处理零分母；不能仅假定输入永远非零。', 'TCSA0040'),
    ('SA0043', '全局变量仅在一个 POU 使用时审查其必要性；HMI/ADS 外部接口不能直接删除或局部化。', 'TCSA0043'),
    ('SA0062', '检查赋值后恒真/恒假条件及恒值表达式，确认是否掩盖逻辑错误。', 'TCSA0062'),
    ('SA0054', 'REAL/LREAL 相等判断使用符合单位和精度的容差。', 'TCSA0054'),
    ('SA0075', 'CASE 提供 ELSE，明确非法状态的处理。', 'TCSA0075'),
    ('SA0076', '枚举 CASE 审阅所有枚举常量的覆盖。', ''),
    ('SA0078', 'CASE 必须有实际分支。', ''),
    ('SA0103', '跨任务非原子数据使用一致性同步或快照协议。', ''),
    ('SA0105', '周期子 FB 按先写输入、无条件调用一次、再读输出组织；这是 Agent 调用规范。', 'fb-call-count / fb-call-zone'),
    ('SA0107', 'FB 调用使用命名参数，让输入输出含义可审查。', ''),
    ('SA0140', '删除废弃的注释代码，保留解释意图的注释。', 'TCSA0140'),
    ('SA0145', '引用使用前验证初始化和有效性。', ''),
    ('SA0167', '需要保留状态的 FB、定时器和沿检测器不得放在 VAR_TEMP。', 'TCSA0167'),
    ('SA0171', '新建枚举使用 strict 属性，遵循项目的限定命名约定。', ''),
    ('SA0172', '数组访问验证上下界；循环边界与实际声明一致。', 'TCSA0172'),
    ('SA0175', '字符串操作核对长度、终止和 API 参数含义。', ''),
    ('SA0178', '官方默认复杂度上限20且可配置；计分含控制流嵌套、布尔运算链、SEL/MUX/JMP，不能用嵌套层数代替。TCSA仍为近似计分。', 'TCSA0178'),
    ('SA0179', '减少模块之间的隐式访问，以明确接口传递数据。', ''),
)


def constraints(rule_id: str = '') -> dict:
    requested = rule_id.strip().upper()
    rows = [dict(reference=ref, requirement=text, checker=checker,
                 coverage='partial_syntax' if checker else 'author_review')
            for ref, text, checker in _RULES if not requested or ref == requested]
    if requested and not rows:
        raise ValueError(f'未收录规则 {requested}；省略 rule_id 可查看完整 Agent 基线')
    return deepcopy(dict(
        engine='TCSA', license_required=False, te1200_executed=False,
        source=SOURCE, rules=rows,
        note='Agent 自有约束和局部语法检查，不覆盖 TE1200 全部规则或编译器语义。',
    ))


def prompt_contract() -> str:
    from .plc_syntax import PROMPT
    requirements = '\n'.join(f'{ref}：{text}' for ref, text, _ in _RULES)
    return (
        '\n\nPLC 写前静态约束（Agent 自有实现，无需 TE1200 授权）：\n'
        + requirements + '\n' + PROMPT
        + '\n首次撰写即遵循上述要求；plc_static_constraints 可查询规则和实际检查覆盖。'
        '写入前执行现有候选代码门禁；写后回读、编译，再用 plc_static_analysis 检查。'
        '发现 error 必须修正，warning 必须审阅；不能用关闭门禁或 pragma 掩盖缺陷。'
        '语义规则由代码审阅补充，工具无告警不代表全部规则已验证。'
        '编译成功不等于通过 TE1200；本流程未执行 TE1200 静态分析，'
        '只能报告 Agent/TCSA 检查结果，不加载或分发反编译程序集，不改变授权校验。'
    )
