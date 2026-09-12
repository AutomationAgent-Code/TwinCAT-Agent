"""Editor-local define/undefine/defined/hasvalue subset; unknown globals stay unknown."""
import re
from .lint import _st_code


def preprocess_local(source, *, variable_lookup=None):
    masked=_st_code(source)
    values={}
    stack=[]
    active=True
    output=[]
    cursor=0
    def blank(text): return re.sub(r'[^\r\n]',' ',text)
    def evaluate(expr):
        # Parse rather than split: quoted values may contain AND/OR or parentheses.
        if len(expr)>8192: raise ValueError('Conditional expression budget exceeded')
        pos=0
        def space():
            nonlocal pos
            while pos<len(expr) and expr[pos].isspace(): pos+=1
        def keyword(word):
            nonlocal pos
            space()
            match=re.match(word+r'\b',expr[pos:],re.I)
            if match: pos+=match.end()
            return bool(match)
        def atom(depth):
            nonlocal pos
            if depth>32: raise ValueError('Conditional nesting budget exceeded')
            if keyword('NOT'): return not atom(depth+1)
            space()
            if expr[pos:pos+1]=='(':
                pos+=1
                result=disjunction(depth+1)
                space()
                if expr[pos:pos+1]!=')': raise ValueError('Missing condition parenthesis')
                pos+=1
                return result
            query=re.match(r'(defined|hastype)\s*\(\s*variable\s*:\s*([A-Za-z_]\w*)\s*(?:,\s*([A-Za-z_]\w*)\s*)?\)',expr[pos:],re.I)
            if query:
                pos+=query.end()
                if (query[1].lower()=='defined') != (query[3] is None):
                    raise ValueError('Malformed variable condition')
                variable=variable_lookup(query[2]) if variable_lookup else None
                if variable is None: raise ValueError('Variable scope evidence unavailable: '+query[2])
                if query[1].lower()=='defined': return True
                from .date_literals import ALIASES
                from .interface_types import string_spec
                actual=ALIASES.get(variable.upper(),variable.upper())
                string=string_spec(actual)
                if string: actual=string[0]
                expected=ALIASES.get(query[3].upper(),query[3].upper())
                primitives=set('BOOL BYTE DATE DINT DWORD INT LDATE LDT LINT LREAL LTIME LTOD LWORD REAL SINT STRING TIME DT TOD ULINT UDINT UINT USINT WORD WSTRING'.split())
                if actual not in primitives or expected not in primitives:
                    raise ValueError('Variable condition requires resolved primitive type')
                return actual==expected
            match=re.match(r"(defined|hasvalue)\s*\(\s*([A-Za-z_]\w*)\s*(?:,\s*'([^']*)'\s*)?\)",expr[pos:],re.I)
            if not match or match[2] not in values: raise ValueError('Compiler definition evidence unavailable')
            pos+=match.end()
            present,value=values[match[2]]
            if match[1].lower()=='defined' and match[3] is not None: raise ValueError('Unsupported condition')
            if match[1].lower()=='hasvalue' and match[3] is None: raise ValueError('Unsupported condition')
            return present if match[1].lower()=='defined' else present and value==match[3]
        def conjunction(depth):
            result=atom(depth)
            while keyword('AND'):
                right=atom(depth)  # Never skip validation of an unknown operand.
                result=result and right
            return result
        def disjunction(depth):
            result=conjunction(depth)
            while keyword('OR'):
                right=conjunction(depth)
                result=result or right
            return result
        result=disjunction(0)
        space()
        if pos!=len(expr): raise ValueError('Unsupported conditional expression suffix')
        return result
    try:
        for match in re.finditer(r'\{[^}]*\}',masked):
            token=source[match.start()+1:match.end()-1].strip()
            head,_,tail=token.partition(' ')
            head=head.upper()
            # Accept arbitrary whitespace between command and arguments.
            parts=token.split(None,1)
            head=parts[0].upper() if parts else ''
            tail=parts[1] if len(parts)>1 else ''
            output.append(source[cursor:match.start()] if active else blank(source[cursor:match.start()]))
            consumed=True
            if head=='IF':
                selected=evaluate(tail) if active else False
                stack.append([active,selected,False])
                active=active and selected
            elif head in {'ELSIF','ELSE'}:
                if head=='ELSE' and tail: raise ValueError('Unexpected ELSE arguments')
                if not stack or stack[-1][2]: raise ValueError('Unexpected conditional branch')
                parent,taken,_=stack[-1]
                selected=(evaluate(tail) if head=='ELSIF' else True) if parent and not taken else False
                active=parent and selected
                stack[-1][1]=taken or selected
                stack[-1][2]=head=='ELSE'
            elif head=='END_IF':
                if tail: raise ValueError('Unexpected END_IF arguments')
                if not stack: raise ValueError('Unexpected END_IF pragma')
                active=stack.pop()[0]
            elif head in {'DEFINE','UNDEFINE'}:
                if active:
                    definition=re.fullmatch(r"(\w+)(?:\s+'([^']*)')?",tail)
                    if not definition or head=='UNDEFINE' and definition[2] is not None: raise ValueError('Unsupported definition')
                    values[definition[1]]=(head=='DEFINE',definition[2])
            else:
                consumed=False
            raw=source[match.start():match.end()]
            output.append(blank(raw) if consumed or not active else raw)
            cursor=match.end()
        if stack: raise ValueError('Unclosed conditional pragma')
        output.append(source[cursor:] if active else blank(source[cursor:]))
        return {'status':'processed','source':''.join(output),'compiler_verified':False}
    except ValueError as exc:
        return {'status':'unknown','source':source,'reason':str(exc),'compiler_verified':False}


def preprocess_editor(source, area='implementation', *, variable_lookup=None):
    """Select local branches only; declaration and implementation never share defines."""
    code=_st_code(source)
    conditional=bool(re.search(r'\{\s*(?:IF|ELSIF|ELSE|END_IF)\b',code,re.I))
    if area=='declaration' and conditional and re.search(r'\bTYPE\b',code,re.I):
        # Officially unsupported for enum components and alias base types.
        # Do not turn those into apparently valid declarations by erasing pragmas.
        header=re.sub(r'\{[^}]*\}','',code).strip()
        if not re.match(r'TYPE\s+\w+\s*:\s*(?:STRUCT|UNION)\b',header,re.I):
            return {'status':'unknown','source':source,'compiler_verified':False,
                    'reason':'Conditional enum components and alias types are not supported by TwinCAT.'}
    result=preprocess_local(source,variable_lookup=variable_lookup if area=='implementation' else None) if '{' in code else {
        'status':'processed','source':source,'compiler_verified':False}
    return {**result,'conditional':conditional,
            'minimum_twincat_version':'3.1.4024.0' if conditional and area=='declaration' else None}


def preprocess_candidate(candidate):
    prepared=dict(candidate)
    evidence={}
    variables_cache=None
    def variable_lookup(name):
        nonlocal variables_cache
        if variables_cache is None:
            from .st_preflight import declarations
            variables_cache={}
            for key in ('enclosing_declaration','declaration'):
                symbols,issues=declarations(prepared.get(key) or '')
                if issues:
                    variables_cache={}
                    break
                variables_cache.update(symbols)
        return variables_cache.get(name.upper(),{}).get('type')
    for key in ('declaration','enclosing_declaration','implementation'):
        if key not in candidate: continue
        result=preprocess_editor(candidate.get(key) or '',
                                 'implementation' if key=='implementation' else 'declaration',
                                 variable_lookup=variable_lookup)
        prepared[key]=result['source']
        evidence[key]={k:v for k,v in result.items() if k!='source'}
    return prepared,evidence
