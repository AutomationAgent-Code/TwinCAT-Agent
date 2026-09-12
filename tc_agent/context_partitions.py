"""Request-only hot evidence / compact history projection.

Never edits durable messages, user requests, assistant tool calls or call IDs.
System instructions, project memory and authorization remain with their owners.
"""
import json

from tc_agent.result_decision import decision
from tc_agent.source_delivery import contains_source

PROTECTED = frozenset({'build_plan_token', 'plans', 'member_baseline', 'document_baseline'})


def project(messages, *, hot_results=2):
    last_user = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=0)
    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    # Preserve the immediately preceding turn too: "yes/continue/option 1" must
    # not lose the evidence just discussed simply because a new user spoke.
    hot = set(tool_indices[-hot_results:]) if hot_results > 0 else set()
    out = []
    for i, message in enumerate(messages):
        value = message.get('result')
        if (message.get('role') != 'tool' or i in hot or not isinstance(value, dict)
                or PROTECTED.intersection(value) or contains_source(value)
                or value.get('context_partition')
                or len(json.dumps(value, ensure_ascii=False)) <= 1800):
            out.append(message)
            continue
        compact = {'context_partition': 'working_evidence' if i >= last_user else 'historical_evidence',
                   'details_omitted': True, 'decision_evidence': decision(value),
                   'next_action': '旧工具正文已移出本次请求，原始结果保留在台账；不得把省略当作成功、授权或缺口消失。'}
        for key in ('status', 'name', 'path', 'tree_path', 'source_path', 'source_hashes',
                    'approved', 'written', 'verified', 'compiler_verified', 'truncated'):
            if key in value:
                compact[key] = value[key]
        if value.get('preflight_summary_version') == 1:
            for key in ('findings_count', 'unsupported_reason_count', 'evidence_missing_count',
                        'candidate_count', 'absence_is_not_clearance'):
                if key in value:
                    compact[key] = value[key]
            compact['candidates'] = [{k: c[k] for k in ('name', 'sha256', 'approved', 'semantic_status',
                'findings_count', 'unsupported_reason_count') if k in c}
                for c in value.get('candidates', [])]
        out.append({**message, 'result': compact})
    return out
