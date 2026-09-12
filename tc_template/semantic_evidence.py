"""Classify incomplete offline evidence without calling it a compiler error."""

UNKNOWN_RULES = {'semantic-unresolved', 'dependency-context-unavailable', 'syntax-unsupported'}

# Explicit, reviewed capabilities only. Never downgrade missing symbols,
# malformed declarations, unsupported syntax or unknown future diagnostics.
DEFERRED_MESSAGES = frozenset({
    'Array bounds compatibility requires exact shape evidence.',
    'Native unsigned integer conversion requires actual target-width evidence; retain __UXINT for XSIZEOF.',
    'Reference binding/lifetime is not fully verified.',
    'Pointer dereference requires address/lifetime evidence; type is known but access safety is not verified.',
    'Non-string constant passed by reference requires evidence of the Replace constants compiler option.',
})


def apply_source_write_policy(findings, semantic_review):
    """Separate offline source acceptance from complete semantic verification."""
    if not semantic_review or semantic_review.get('syntax_status') != 'parsed':
        return list(findings)
    return [{**f, 'severity': 'warning', 'original_severity': f.get('severity'),
             'verification_required': True, 'write_policy': 'deferred_semantic_evidence'}
            if f.get('rule') == 'semantic-unresolved' and f.get('message') in DEFERRED_MESSAGES
            else f for f in findings]


def classify(findings):
    incomplete = [f for f in findings if f.get('rule') in UNKNOWN_RULES]
    errors = [f for f in findings if f.get('severity') == 'error' and f.get('rule') not in UNKNOWN_RULES]
    return {'status': 'invalid' if errors else 'incomplete' if incomplete else 'verified_subset',
            'code_error_count': len(errors), 'unverified_count': len(incomplete),
            'unsupported_reasons': list(dict.fromkeys(f.get('message', '') for f in incomplete)),
            'compiler_verified': False,
            'next_action': ('存在未验证项：仅在统一写入策略允许、语法结构通过且无其他阻断时继续离线源码流程；保留未验证项并取得真实编译证据，不据此上线。未降级的缺失依赖仍须补齐，禁止拆分候选或换工具绕过。'
                            if incomplete else '仅覆盖已实现的离线规则，不替代真实编译。')}
