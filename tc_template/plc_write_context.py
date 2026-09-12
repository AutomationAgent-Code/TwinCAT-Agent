"""Bounded live dependency checks, not an ST compiler or online validation."""
from __future__ import annotations

import re
from .lint import _st_code

_ID = r'[A-Za-z_]\w*'
_INTEGER = {'SINT', 'USINT', 'INT', 'UINT', 'DINT', 'UDINT', 'LINT', 'ULINT'}


def non_ascii_literal(text):
    """Skip nested comments; only STRING/WSTRING literals need encoding review."""
    i, depth = 0, 0
    while i < len(text):
        pair = text[i:i + 2]
        if pair == '(*':
            depth += 1
            i += 2
        elif depth:
            if pair == '*)':
                depth -= 1
                i += 1
            i += 1
        elif pair == '//':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
        elif text[i] in "'\"":
            quote, start = text[i], i
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == '$' else 1
            if quote == "'" and any(ord(c) > 127 for c in text[start:i]):
                return True
            i += 1
        else:
            i += 1
    return False


def variables(declaration):
    """Extract simple declarations only; unsupported types remain unresolved."""
    from .conditional_compilation import preprocess_editor
    selected=preprocess_editor(declaration,'declaration')
    if selected['status']!='processed': return {}
    declaration=selected['source']
    code = _st_code(declaration)
    return {name.strip().upper(): match[1].upper()
            for match in re.findall(rf'\b({_ID}(?:\s*,\s*{_ID})*)\s*:\s*({_ID})\b', code)
            for name in match[0].split(',')}


def review_dependencies(candidate, path, call, *, limit=12, semantic=False):
    from .conditional_compilation import preprocess_candidate
    original_candidate=candidate
    candidate,preprocessing=preprocess_candidate(candidate)
    # An unknown branch is not evidence of a bad reference in that branch.
    code = _st_code(candidate.get('implementation', '')) if preprocessing.get('implementation',{}).get('status')=='processed' else ''
    decl = candidate.get('declaration', '')
    local = variables(decl)
    findings, unknown, evidence = [], [], []
    unknown.extend(v['reason'] for v in preprocessing.values() if v['status']!='processed')
    cache = {}
    library_index = None  # Per-review cache; never reuse across PID/solution/version changes.
    qualified_index = None
    settings_cache=None
    settings_attempted=False
    parts = str(path).split('^')
    scope = '^'.join(parts[:3]) + '^' if len(parts) >= 4 and parts[0] == 'TIPC' else ''
    owner_decl = candidate.get('enclosing_declaration') or decl
    owner_match = re.search(r'\bFUNCTION_BLOCK\s+(?:(?:FINAL|ABSTRACT)\s+)*([A-Za-z_]\w*)', _st_code(owner_decl), re.I)
    # Bind self to the exact candidate parent, not a global name search that
    # could select a same-named object or the old on-disk declaration.
    if owner_match and scope:
        owner_name = owner_match[1]
        owner_indices = [i for i in range(3, len(parts)) if parts[i].casefold() == owner_name.casefold()]
        if len(owner_indices) == 1:
            cache[owner_name.upper()] = {'name': owner_name, 'declaration': owner_decl,
                                        'path': '^'.join(parts[:owner_indices[0] + 1])}

    def finding(rule, message, severity='error'):
        findings.append(dict(rule=rule, severity=severity, area='implementation',
                             object=candidate.get('name', ''), message=message))

    def resolve_settings():
        nonlocal settings_cache,settings_attempted
        if settings_attempted: return settings_cache
        settings_attempted=True
        if not scope: return None
        try:
            result=call('compiler-settings',path=scope.rstrip('^'))
            if (result.get('status')=='read' and result.get('path')==scope.rstrip('^')
                    and result.get('source')=='live_compiler_settings_xml'):
                settings_cache=result
        except (RuntimeError,ValueError,KeyError,OSError):
            pass
        return settings_cache

    def resolve_qualified(name):
        nonlocal qualified_index
        if qualified_index is None:
            qualified_index={}
            try:
                refs=call('library-evidence',path=scope.rstrip('^'))
                signatures=call('library-signatures',path=scope.rstrip('^'))
                if refs.get('status')=='read' and signatures.get('status')=='read':
                    for ref in refs.get('libraries',[]):
                        namespace=ref.get('namespace','')
                        if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*',namespace): continue
                        for lib in signatures.get('libraries',[]):
                            if lib.get('name')!=ref.get('name') or lib.get('version')!=ref.get('effective_version'): continue
                            for obj in lib.get('objects',[]):
                                if obj.get('signature_complete') or obj.get('opaque_type'):
                                    qualified_index.setdefault((namespace+'.'+obj['name']).upper(),[]).append(obj)
            except (RuntimeError,ValueError,KeyError,OSError):
                pass
        matches=qualified_index.get(name.upper(),[])
        return matches[0] if len(matches)==1 else None

    def resolve(name):
        nonlocal library_index
        key = name.upper()
        if key in cache:
            return cache[key]
        cache[key] = None
        if not scope or len(cache) > limit:
            unknown.append(name + ': project scope unavailable or dependency budget exceeded')
            return None
        if '.' in name:
            obj=resolve_qualified(name)
            if obj:
                cache[key]=obj
                evidence.append({k:obj[k] for k in ('name','library','version','source')})
                return obj
            unknown.append(name+': qualified library identity/version unavailable or ambiguous')
            return None
        try:
            found = call('find-pou', query=name, include_members=False, limit=200)
            matches = [m for m in found.get('matches', [])
                       if str(m.get('name', '')).upper() == key
                       and str(m.get('path', '')).casefold().startswith(scope.casefold())]
            if (semantic and not matches and not found.get('matches') and
                    not found.get('truncated') and found.get('total', 0) == 0):
                if library_index is None:
                    library_index = {}
                    libraries = call('library-signatures', path=scope.rstrip('^'))
                    if libraries.get('status') == 'read':
                        for lib in libraries.get('libraries', []):
                            for obj in lib.get('objects', []):
                                library_index.setdefault(obj['name'].upper(), []).append(obj)
                library_matches = library_index.get(key, [])
                if len(library_matches) == 1 and (library_matches[0].get('signature_complete') or library_matches[0].get('opaque_type')):
                    obj = library_matches[0]
                    cache[key] = obj
                    entry = {k: obj[k] for k in ('name', 'library', 'version', 'source')}
                    if obj.get('interface_knowledge'):
                        entry['interface_knowledge'] = obj['interface_knowledge']
                    evidence.append(entry)
                    return obj
            if len(matches) != 1 or found.get('truncated') or found.get('total', 0) > 200:
                unknown.append(name + ': dependency missing, ambiguous or search incomplete')
                return None
            obj = call('read-pou', name=matches[0]['name'], path=matches[0]['path'],
                       area='declaration', include_member_code=False, start_line=1, max_lines=0)
            if 'declaration' not in obj or obj.get('declaration_paging', {}).get('truncated'):
                raise ValueError('incomplete declaration')
            from .conditional_compilation import preprocess_editor
            from .compiler_context import source_version_requirements
            requirements=source_version_requirements(obj)
            selected=preprocess_editor(obj['declaration'],'declaration')
            if selected['status']!='processed':
                raise ValueError(selected['reason'])
            obj={**obj,'declaration':selected['source'],'source_version_requirements':requirements}
            obj = {**obj, 'path': matches[0]['path']}
            cache[key] = obj
            evidence.append({'name': name, 'path': matches[0]['path'], 'source': 'live_com'})
            return obj
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            unknown.append(name + ': ' + str(exc)[:240])
            return None

    member_cache = {}

    def resolve_member(root, member):
        key = (root.upper(), member.upper())
        if key in member_cache:
            return member_cache[key]
        member_cache[key] = None
        parent = resolve(root)
        if not parent or len(member_cache) > limit:
            return None
        if parent.get('source') == 'live_library_signatures':
            return None  # No method declarations in this supported XML subset.
        base=re.search(r'\bFUNCTION_BLOCK\s+(?:(?:FINAL|ABSTRACT)\s+)*\w+\s+EXTENDS\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)',_st_code(parent.get('declaration','')),re.I)
        actual_member=member
        if base:
            inventory=parent.get('member_catalog')
            if inventory is None:
                try:
                    inventory=call('read-pou',name=root,path=parent['path'],area='members',include_member_code=False).get('member_catalog')
                except (RuntimeError,ValueError,KeyError,OSError):
                    inventory=None
            if inventory and inventory.get('complete') is True:
                matches=[entry for entry in inventory.get('entries',[]) if entry.get('name','').casefold()==member.casefold()]
                if not matches:
                    inherited=resolve_member(base[1],member)
                    if inherited:
                        member_cache[key]={**inherited,'inherited_from':inherited.get('declaring_type',base[1])}
                    return member_cache[key]
                if len(matches)!=1:
                    unknown.append(root+'.'+member+': member inventory is ambiguous')
                    return None
                actual_member=matches[0]['relative_path']
        try:
            obj = call('read-pou', name=root, path=parent['path'], method=actual_member,
                       area='declaration', include_member_code=False, start_line=1, max_lines=0)
            if 'declaration' in obj and not obj.get('declaration_paging', {}).get('truncated'):
                member_cache[key] = {**obj,'declaring_type':root}
                evidence.append({'name': root + '.' + member, 'path': parent['path'],
                                 'method': member, 'source': 'live_com'})
        except (RuntimeError, ValueError, KeyError, OSError):
            pass
        return member_cache[key]

    qualified = {}
    # Enum member references occur in CASE labels and conditions as well as
    # assignments. Resolve the actual project enum before validating a member.
    for root, member in sorted(set(re.findall(rf'\b(E_\w+)\s*\.\s*({_ID})', code, re.I))):
        if root.upper() in local:
            continue
        obj = resolve(root)
        if obj is None:
            continue
        enum_body = re.search(r'\bTYPE\s+\w+\s*:\s*\((.*?)\)', _st_code(obj['declaration']), re.I | re.S)
        if enum_body:
            members = {m.upper() for m in re.findall(r'(?:^|,)\s*([A-Za-z_]\w*)', enum_body[1])}
            if member.upper() not in members:
                finding('dependency-enum-member-missing', f'{root}.{member} is absent from the live enum declaration.')
    for root, member in sorted(set(re.findall(rf'\b(GVL_\w+)\s*\.\s*({_ID})', code, re.I))):
        if root.upper() in local:  # a local instance can shadow a GVL name
            continue
        obj = resolve(root)
        if obj is None:
            continue
        members = variables(obj['declaration'])
        if member.upper() not in members:
            finding('dependency-member-missing', f'{root}.{member} is absent from the live GVL declaration.')
        else:
            qualified[(root + '.' + member).upper()] = members[member.upper()]

    # Only complete scalar assignments are checked. Calls, pointers, arrays,
    # inherited properties and arbitrary expressions are intentionally excluded.
    for lhs, rhs in re.findall(rf'\b({_ID}(?:\.{_ID})?)\s*:=\s*({_ID}(?:\.{_ID})?|[+-]?\d+)\s*;', code):
        target_type = local.get(lhs.upper()) or qualified.get(lhs.upper())
        if not target_type or not target_type.startswith('E_'):
            continue
        enum = resolve(target_type)
        if enum is None:
            continue
        if not re.search(r'\bTYPE\s+\w+\s*:\s*\(', _st_code(enum['declaration']), re.I):
            continue
        source_type = local.get(rhs.upper()) or qualified.get(rhs.upper())
        if source_type in _INTEGER:
            finding('dependency-enum-assignment', f'{lhs}: cannot implicitly assign {source_type} {rhs} to enum {target_type}; use explicit enum mapping.')

    if re.search(r'\bVAR(?:_INPUT|_OUTPUT|_IN_OUT|_TEMP|_GLOBAL)?\b|\bEND_VAR\b', code, re.I):
        finding('implementation-declaration', 'VAR declarations belong in the declaration area, not implementation.')
    if re.search(r'\b(?:SINT|USINT|INT|UINT|DINT|UDINT|LINT|ULINT)#\s*\(', code, re.I):
        finding('invalid-typed-literal', 'TYPE#(...) is not a numeric literal; use a supported explicit conversion function.')
    # No guessed compiler encoding: this is a prerequisite for review, not a
    # claim that all non-ASCII strings fail (UTF-8 may be enabled).
    raw = candidate.get('implementation', '')
    if non_ascii_literal(raw) or non_ascii_literal(decl):
        finding('encoding-review', 'Non-ASCII STRING literal: verify encoding against actual project/compiler options before build; encoding is not verified by this check.', 'warning')
    semantic_review = None
    if semantic:
        from .st_preflight import review_candidate
        semantic_review = review_candidate(original_candidate, resolve, resolve_member, resolve_qualified=resolve_qualified, resolve_settings=resolve_settings)
        findings.extend(semantic_review['findings'])
    if unknown:
        finding('dependency-context-unavailable', '; '.join(unknown))
    from .semantic_evidence import classify, apply_source_write_policy
    findings = apply_source_write_policy(findings, semantic_review)
    return {'findings': findings, 'dependencies': evidence, 'unresolved': unknown,
            'compiler_settings':settings_cache,
            'semantic_evidence': classify(findings),
            'semantic_review': semantic_review,
            'source': 'live_com', 'compiler_verified': False,
            'coverage': 'Offline syntax and known semantic checks; explicitly classified deeper evidence may defer source acceptance, never compiler verification.' if semantic else
                        'GVL_ direct members and E_ enum scalar assignments only; no library/inheritance/array/expression type resolution'}
