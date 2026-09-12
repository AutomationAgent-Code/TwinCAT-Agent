"""Conservative saved-source checks; never claim live PLC or HMI verification."""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from tc_agent.execution_policy import tool_succeeded


HMI_ACCEPTANCE_TOOLS = {
    'tc_hmi_build', 'tc_hmi_validate', 'tc_hmi_bindings',
    'tc_hmi_binding_diagnose', 'tc_hmi_ads_live_check',
    'tc_hmi_browser_validate',
}


_FINAL_CLAIMS = (
    re.compile(r"(?:编译|构建)\s*(?:已经|已)?(?:成功|通过)|build\s+succeeded|compile(?:d)?\s+successfully", re.I),
    re.compile(r"(?:代码|程序|语法)\s*(?:已经|已)?(?:正确|无误)|syntax\s+is\s+correct", re.I),
    re.compile(r"(?:可以|能够|允许|适合)\s*(?:直接)?(?:部署|上线)|deployable", re.I),
    re.compile(r"(?:任务|本次(?:修改|修复)|所有问题|全部问题|工程)\s*(?:已经|已)?(?:完成|修复)|all\s+(?:issues|errors)\s+(?:are\s+)?resolved", re.I),
    re.compile(r"(?:验证|验收)\s*(?:已经|已)?(?:通过|完成)|verification\s+(?:passed|complete)", re.I),
)
_NEGATION = re.compile(r"(?:未|不|无法|不能|尚未|仍未|未能|没有|失败|待|not|cannot|can't|unable)\s*$", re.I)


def completion_claims(text: str) -> list[str]:
    """Find positive engineering-completion claims, excluding nearby negation."""
    value = str(text or "")
    claims: list[str] = []
    for pattern in _FINAL_CLAIMS:
        for match in pattern.finditer(value):
            prefix = value[max(0, match.start() - 12):match.start()]
            if _NEGATION.search(prefix):
                continue
            claims.append(match.group(0))
    return claims


def plc_entry_evidence(project):
    from tc_agent.generation_context import _text
    project = Path(project)
    root = ET.fromstring(_text(project))
    programs, entries = {}, []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] != 'Compile':
            continue
        path = (project.parent / node.get('Include', '').replace('\\', '/')).resolve()
        if path.suffix.lower() not in {'.tcpou', '.tctto'}:
            continue
        doc = ET.fromstring(_text(path))
        if path.suffix.lower() == '.tctto':
            entries.extend(n.findtext('Name', '') for n in doc.iter('PouCall'))
        else:
            pou = doc.find('POU')
            if pou is not None and re.search(r'\bPROGRAM\s+\w+', pou.findtext('Declaration', ''), re.I):
                source = pou.findtext('Implementation/ST', '')
                source = re.sub(r"\(\*.*?\*\)|//[^\n]*|'(?:\$\'.|[^'])*'", ' ', source, flags=re.S)
                programs[pou.get('Name', '').casefold()] = (pou.get('Name'), source)
    reachable = {name.casefold() for name in entries}
    for _ in programs:
        before = set(reachable)
        for name in before & programs.keys():
            reachable.update(target for target in programs if re.search(
                r'(?<![\w.])' + re.escape(target) + r'\s*\(', programs[name][1], re.I))
        if before == reachable:
            break
    return {'project': str(project), 'task_entries': entries,
            'unreferenced_programs': [item[0] for name, item in programs.items() if name not in reachable],
            'empty_entry_programs': [programs[name][0] for name in reachable & programs.keys()
                                     if not programs[name][1].strip()],
            'scope': 'saved_direct_program_calls_only', 'live_verified': False,
            'note': '间接/FB 方法内调用及未保存修改需要另行精读，不可仅凭此检查认定不可达或可运行。'}


class CompletionEvidence:
    def __init__(self):
        self.changed = set()
        self.failed = {}
        self.blocked = {}
        self.checked = set()
        self.hmi_checks = {}
        self.hmi_changed_views = set()

    def record(self, name, args, result, readonly):
        if not name.startswith(('plc_', 'tc_hmi_')):
            return
        domain = 'hmi' if name.startswith('tc_hmi_') else 'plc'
        key = (name, str(args.get('project', '')), str(args.get('file') or args.get('name') or ''))
        ok = tool_succeeded(result)
        if name == 'plc_build':
            from tc_template.plc_build_diagnostics import build_diagnostics
            result = build_diagnostics(result)
            ok = result['compiler_verified']
            if not ok:
                self.checked.discard(name)
        if name == 'plc_static_analysis':
            errors = (result.get('summary') or {}).get('errors')
            ok = ok and type(errors) is int and errors == 0
            if not ok:
                # A successful tool invocation is not a successful analysis.
                self.checked.discard(name)
                result = dict(result)
                result.setdefault('reason', f'Agent 静态分析仍有 {errors} 个错误' if type(errors) is int
                                  else 'Agent 静态分析缺少完整错误数量，不能声明通过')
        result = dict(result or {})
        blocked = (
            result.get('authorization_blocked') is True
            or result.get('not_executed') is True and str(result.get('status', '')).lower()
            in {'denied', 'blocked', 'review_required'}
            or 'denied' in result
            or str(result.get('status', '')).lower() in {'denied', 'blocked'}
        )
        # Preserve failed acceptance results as evidence too.  Previously a
        # blocked binding diagnosis disappeared because it is a read-only tool
        # whose name does not end in "validate".  That let a later model turn
        # misdescribe a static expression failure as a server restart issue.
        if domain == 'hmi' and name in HMI_ACCEPTANCE_TOOLS:
            self.hmi_checks[name] = dict(result)
        if blocked:
            blocked_reason = str(
                result.get('reason') or result.get('error') or result.get('denied')
                or result.get('next_action') or '操作被权限/门禁拒绝，未执行'
            )[:220]
            if '未执行' not in blocked_reason and '未运行' not in blocked_reason:
                blocked_reason = '操作未执行：' + blocked_reason
            self.blocked[key] = blocked_reason[:240]
            self.failed.pop(key, None)
        elif not ok:
            # Failed exploratory reads do not invalidate the product. Failed
            # writes and explicit acceptance checks must remain visible.
            if (not readonly or result.get('warning_review_required') is True
                    or name.endswith(('build', 'validate', 'verify', 'bindings', 'analysis'))):
                self.failed[key] = str(result.get('reason') or result.get('next_action')
                                       or result.get('error') or result.get('status')
                                       or '验证不完整')[:240]
        else:
            self.failed.pop(key, None)
        # Build and previews do not count as source changes. Invalidate verification
        # after every successful write, including a later edit in the same run.
        source_write = (result.get('written') is True or str(result.get('status', '')).lower()
                        in {'applied', 'created', 'written', 'patched', 'deleted', 'removed', 'renamed', 'restored'})
        if ((ok or result.get('written') is True or result.get('status') == 'uncertain')
                and (source_write or result.get('status') == 'uncertain') and not readonly and not name.endswith(('build', 'verify', 'snapshot'))
                and name not in {'plc_write_values', 'plc_write_value'}
                and args.get('action') != 'read' and args.get('apply') is not False):
            self.changed.add(domain)
            self.checked = {n for n in self.checked if not n.startswith('tc_hmi_' if domain == 'hmi' else 'plc_')}
            if domain == 'hmi':
                self.hmi_checks.clear()
                changed_file = str(args.get('file') or args.get('name') or '').replace('\\', '/')
                if changed_file.lower().endswith('.view'):
                    self.hmi_changed_views.add(changed_file.casefold())
        elif ok:
            self.checked.add(name)
            if name.startswith('tc_hmi_') and name not in HMI_ACCEPTANCE_TOOLS:
                self.hmi_checks[name] = dict(result)
            if name == 'tc_hmi_bindings' and result.get('binding_count') == 0:
                self.failed[key] = '当前绑定数量为 0；仅可声明静态页面有效，不能声明 PLC 通信已接通。'
            elif name == 'tc_hmi_bindings' and result.get('warning_count', 0):
                self.failed[key] = '绑定仍有未映射/未确认项；必须核对符号，不能宣称运行时可用。'

    def retryable_validation_gap(self, solution):
        """Only retry a real post-write evidence gap once; never retry a gate."""
        return bool(self.changed) and not self.blocked and bool(self.issues(solution))

    def issues(self, solution):
        # A failed write/build must block a success claim even when no source
        # mutation ever completed.  The old early return only protected runs
        # after at least one successful write, so an entirely rejected task
        # could still be summarized as completed.
        if not self.changed and not self.failed and not self.blocked:
            return []
        issues = [f'{key[0]}：{reason}' for key, reason in self.blocked.items()]
        issues.extend(f'{key[0]}：{reason}' for key, reason in self.failed.items())
        if not self.changed:
            return issues[:12]
        if 'plc' in self.changed:
            if 'plc_build' not in self.checked:
                issues.append('最后一次 PLC 修改后缺少成功的编译及完整诊断证据。')
            if not self.checked.intersection({'plc_static_analysis', 'plc_verify'}):
                issues.append('最后一次 PLC 修改后缺少静态分析；编译不能证明握手、暂停和计数正确。')
            try:
                from tc_agent.plc_source import discover_projects
                projects = discover_projects(solution)
                if not projects:
                    issues.append('无法确认 PLC 工程及任务入口。')
                for project in projects:
                    evidence = plc_entry_evidence(project)
                    for name in evidence['empty_entry_programs']:
                        issues.append(f'{project.name} 任务调用的 {name} 实现为空。')
                    if evidence['unreferenced_programs']:
                        issues.append(f"{project.name} 未在保存的直接调用链发现：" + ', '.join(evidence['unreferenced_programs']) + '；需确认用途及实际调用路径。')
            except Exception:
                issues.append('保存的 PLC 调用链检查不可用，不能据此宣称程序可运行。')
        if 'hmi' in self.changed:
            required = ('tc_hmi_build', 'tc_hmi_validate', 'tc_hmi_browser_validate')
            for tool in required:
                if tool not in self.checked:
                    issues.append(f'最后一次 HMI 修改后缺少 {tool} 的完整验证。')
            diagnosis = self.hmi_checks.get('tc_hmi_binding_diagnose', {})
            if ('tc_hmi_bindings' not in self.checked
                    and diagnosis.get('static_verified') is not True):
                issues.append('最后一次 HMI 修改后缺少 tc_hmi_bindings 或 tc_hmi_binding_diagnose 的静态绑定证据。')
            build = self.hmi_checks.get('tc_hmi_build', {})
            if build and not (build.get('build_succeeded') is True and
                              build.get('diagnostics_available') is True and
                              build.get('diagnostics_complete') is True):
                issues.append('HMI 构建结果缺少完整诊断；ErrorItems 不可读或不完整不能按 0 错误处理。')
            validation = self.hmi_checks.get('tc_hmi_validate', {})
            if validation and validation.get('valid') is not True:
                issues.append('HMI 保存结构/Schema 校验未通过。')
            bindings = self.hmi_checks.get('tc_hmi_bindings', {})
            if bindings and bindings.get('valid') is not True:
                issues.append('HMI 静态绑定校验未通过。')
            if diagnosis and diagnosis.get('verified') is not True:
                issues.append('HMI 绑定诊断尚未通过：' + str(diagnosis.get('blocking_stage') or '原因未分类'))
            browser = self.hmi_checks.get('tc_hmi_browser_validate', {})
            if browser and not (browser.get('success') is True and
                                browser.get('saved_target_page_verified') is True):
                issues.append('HMI 浏览器检查未验证指定的已保存目标页面。')
            loaded_view = str(browser.get('loaded_view') or '').replace('\\', '/').casefold()
            if browser and self.hmi_changed_views and loaded_view not in self.hmi_changed_views:
                issues.append('浏览器验证的页面不是本轮修改的 View：' + ', '.join(sorted(self.hmi_changed_views)))
            binding_count = max(int(bindings.get('binding_count') or 0),
                                int(diagnosis.get('binding_count') or 0))
            if binding_count > 0:
                live = (self.hmi_checks.get('tc_hmi_ads_live_check', {})
                        or (diagnosis.get('online') if diagnosis.get('verified') is True else {})
                        or {})
                if live.get('verified') is not True:
                    issues.append('HMI 存在 PLC 绑定，但最后一次修改后缺少 tc_hmi_ads_live_check 只读在线证据。')
        return issues[:12]

    def instruction(self, solution):
        issues = self.issues(solution)
        if not issues:
            return ''
        return '\n工程验收证据（后端检查，不是新增授权）：\n' + '\n'.join('- ' + s for s in issues) + (
            '\n先解决范围内缺项；无法解决则明确报告未完成，不得仅说创建完成或只差下载。'
            '历史门禁拦截如已由其他工具纠正，说明纠正证据。不得为验证自动上线。\n')

    def final_note(self, solution):
        issues = self.issues(solution)
        if not issues:
            return ''
        return '\n\n工程验收尚未闭环（自动检查）：\n' + '\n'.join('- ' + s for s in issues) + '\n以上不是设备在线验证；不得据此直接上线。'

    def rejected_final(self, solution):
        """Deterministic user-visible replacement for an unaccepted draft."""
        note = self.final_note(solution)
        if not note:
            return ''
        return (
            '工程验收未通过，TwinCAT Agent 已撤回模型生成的“完成/成功”草稿。'
            + note
            + '\n请以以上后端工具证据为准；修复并重新验证前，不得宣称构建、绑定或页面运行成功。'
        )

    def guard_final(self, text, solution):
        """Keep final text consistent with recorded evidence."""
        claims = completion_claims(text)
        issues = self.issues(solution)
        if not claims or not issues:
            if issues and str(text or "").strip():
                return str(text).rstrip() + self.final_note(solution), False
            return str(text or ""), False
        return self.rejected_final(solution), True
