"""Decision-preserving model projection of full-candidate preflight evidence."""
import json


def summarize(result, limit=12000):
    if not isinstance(result, dict) or result.get('preflight_summary_version') == 1:
        return result
    if result.get('_capped'):
        return {'preflight_summary_version': 1, 'status': result.get('status', 'unknown'),
                'evidence_missing_count': 1, 'details_omitted': True,
                'absence_is_not_clearance': True,
                'next_action': '旧摘要已丢失完整预检判据，不能推断通过或能力缺口消失。报告证据不足；禁止用最小或拆分候选冒充完整审核。'}
    if not isinstance(result.get('candidates'), list):
        return result
    out = {k: result[k] for k in ('status', 'approved', 'written', 'compiler_verified',
                                 'write_authorized', 'error_policy', 'capability_exhausted',
                                 'recovery_exhausted') if k in result}
    out.update(preflight_summary_version=1, candidate_count=len(result['candidates']),
               candidates=[], findings_count=0, unsupported_reason_count=0,
               evidence_missing_count=0, details_omitted=False,
               next_action='按完整候选的错误位置修正；字段或详情被省略不代表问题消失。禁止用最小/拆分候选替代完整审核，也不要重复调用以获取被省略的同一结果；缺少判据时报告证据不足。')
    entries = []
    for c in result['candidates']:
        review = c.get('review') or {}
        evidence = review.get('semantic_evidence') or {}
        findings = review.get('findings')
        reasons = evidence.get('unsupported_reasons')
        out['evidence_missing_count'] += int(not evidence or not isinstance(findings, list))
        out['findings_count'] += len(findings or [])
        out['unsupported_reason_count'] += len(reasons or [])
        item = {k: c[k] for k in ('name', 'path', 'sha256', 'approved') if k in c}
        item.update(semantic_status=evidence.get('status', 'unknown'),
                    findings_count=len(findings) if isinstance(findings, list) else None,
                    unsupported_reason_count=len(reasons) if isinstance(reasons, list) else None,
                    findings=[], unsupported_reasons=[str(r)[:240] for r in (reasons or [])[:3]])
        for f in sorted(findings or [], key=lambda f: f.get('severity') != 'error')[:4]:
            item['findings'].append({k: (v[:240] if isinstance(v, str) else v)
                                     for k, v in f.items()
                                     if k in ('rule', 'severity', 'area', 'line', 'column', 'message')})
        item['details_omitted'] = (len(findings or []) > len(item['findings'])
                                   or len(reasons or []) > len(item['unsupported_reasons'])
                                   or any(len(str(r)) > 240 for r in reasons or [])
                                   or any(len(str(f.get('message', ''))) > 240 for f in findings or []))
        entries.append(item)
    # Failed candidates first; aggregate counts cover every candidate even when
    # individual entries cannot fit. Never pass this projection to the generic
    # dictionary-key truncator, including on later history compactions.
    for item in sorted(entries, key=lambda x: x.get('approved') is True):
        out['candidates'].append(item)
        if len(json.dumps(out, ensure_ascii=False)) > max(1800, limit):
            # Do not discard the only failed candidate merely because paths
            # and diagnostic prose are long. Keep its verdict and first location.
            item['findings'] = item['findings'][:1]
            item['unsupported_reasons'] = [r[:100] for r in item['unsupported_reasons'][:1]]
            for key in ('name', 'path'):
                if isinstance(item.get(key), str) and len(item[key]) > 160:
                    item[key] = item[key][:160] + '…'
                    item['identity_truncated'] = True
            for finding in item['findings']:
                if isinstance(finding.get('message'), str):
                    finding['message'] = finding['message'][:120]
            item['details_omitted'] = True
            out['details_omitted'] = True
        if len(json.dumps(out, ensure_ascii=False)) > max(1800, limit) and len(out['candidates']) > 1:
            out['candidates'].pop()
            out['details_omitted'] = True
            break
        out['details_omitted'] |= item['details_omitted']
    out['omitted_candidates'] = out['candidate_count'] - len(out['candidates'])
    out['absence_is_not_clearance'] = True
    return out
