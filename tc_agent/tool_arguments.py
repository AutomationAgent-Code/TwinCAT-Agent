"""Validate tool requests before host probes, permissions or execution.

Errors describe fields, never echo arbitrary argument values (which may contain
credentials or source code). Open nested objects such as HMI properties remain
open when their schema allows them.
"""
from itertools import islice

from jsonschema import Draft202012Validator


def argument_failure(name, schema, issues):
    result = {
        'status': 'invalid_arguments', 'error_type': 'tool_arguments',
        'error': '工具参数不符合契约；请修正后重试。',
        'tool': name, 'not_executed': True, 'issues': issues,
        'required_parameters': schema.get('required', []),
        'allowed_parameters': sorted(schema.get('properties', {})),
        'next_action': '按当前工具 Schema 修正参数；本次未访问 XAE 或 PLC。',
    }
    if name in {'plc_read_value', 'plc_write_value', 'plc_read_values', 'plc_write_values'}:
        result['next_action'] = (
            '使用 name 指定已核实的 ADS 符号名，例如 MAIN.fbPid.bEnable（仅格式示例）。'
            'TIPC^… 是 XAE 工程树路径，不能作为 ADS 符号，也不能自动截短转换。'
            '先读取实际实例声明/符号清单确认完整名称；批量工具的每项也使用 name。'
        )
    elif name == 'tc_hmi_control_events':
        from tc_template.hmi_events import action_contract
        result['action_contract'] = action_contract()
        result['next_action'] = (
            'actions 中的动作使用 objectType，不使用 actionType/type/action 代替；'
            'value 按 action_contract 的类型化对象格式填写。先 action=read 核实事件。'
        )
    return result


def validate_arguments(name, args, schema):
    # Tool envelopes have a finite contract. Do not silently discard a typo
    # such as path instead of name; user-defined dictionaries remain untouched.
    contract = dict(schema)
    contract.setdefault('additionalProperties', False)
    issues = []
    for error in islice(Draft202012Validator(contract).iter_errors(args), 12):
        field = '.'.join(str(p) for p in error.absolute_path) or '$'
        issue = {'field': field, 'rule': error.validator}
        if error.validator == 'required':
            issue['missing'] = [k for k in error.validator_value if k not in error.instance]
        elif error.validator == 'additionalProperties' and isinstance(error.instance, dict):
            issue['unexpected'] = sorted(set(error.instance) - set(error.schema.get('properties', {})))
        elif error.validator in {'type', 'enum', 'minimum', 'maximum', 'minItems', 'maxItems', 'minLength'}:
            issue['expected'] = error.validator_value
        if issue not in issues:
            issues.append(issue)
    return argument_failure(name, schema, issues) if issues else None


class BatchFailureCounter:
    """A batch of invalid calls gets one correction attempt, not N strikes.

    Construct per model response; carry the returned streak across responses.
    Real execution failures still count individually and successes reset it.
    """
    def __init__(self):
        self.invalid_counted = False

    def record(self, streak, result, ok):
        if ok:
            return 0
        if (isinstance(result, dict) and result.get('error_type') == 'tool_arguments'
                and result.get('not_executed') is True):
            if self.invalid_counted:
                return streak
            self.invalid_counted = True
        return streak + 1
