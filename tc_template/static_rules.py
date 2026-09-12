"""Independent, bounded ST analysis guided by the public Beckhoff rule specs.

No vendor assemblies are loaded. Unknown expressions remain unknown; reports
describe the supplied source set, not a compiler-resolved application/library.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


def mask(text):
    from .lint import _st_code
    return _st_code(text)


@dataclass(frozen=True)
class Token:
    value: str
    offset: int


_LEX = re.compile(r'(?:U?S?INT|DINT|UDINT|LINT|ULINT|REAL|LREAL|BYTE|WORD|DWORD|LWORD)#(?:\d+#)?[+-]?[\dA-Fa-f_]+(?:\.\d+)?(?:[eE][+-]?\d+)?|(?:2|8|16)#[\dA-Fa-f_]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_]\w*|:=|=>|<>|<=|>=|\.\.|\S', re.I)


def tokens(text):
    return [Token(m.group().upper(), m.start()) for m in _LEX.finditer(mask(text))]


def declarations(text):
    """Parse VAR sections, retaining positions and initial expressions."""
    clean = mask(text)
    result = []
    for block in re.finditer(r'\b(VAR(?:_INPUT|_OUTPUT|_IN_OUT|_GLOBAL|_TEMP|_EXTERNAL)?)(\s+(?:CONSTANT|RETAIN|PERSISTENT))?\b(.*?)\bEND_VAR\b', clean, re.I | re.S):
        scope = block[1].upper()
        constant = 'CONSTANT' in (block[2] or '').upper()
        for var in re.finditer(r'\b([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:AT\s+%[^:;]+)?\s*:\s*([^;]+);', block[3], re.I):
            typ, _, initial = var[2].partition(':=')
            for name in var[1].split(','):
                result.append(dict(name=name.strip().upper(), display=name.strip(), type=typ.strip().upper(),
                                   initial=initial.strip(), scope=scope, constant=constant,
                                   line=clean.count('\n', 0, block.start(3) + var.start()) + 1))
    return result


def areas(obj):
    yield 'implementation', str(obj.get('implementation') or ''), []
    for member in obj.get('methods') or []:
        if isinstance(member, dict):
            yield ('member:' + str(member.get('name') or member.get('path') or 'member'),
                   str(member.get('implementation') or member.get('code') or ''),
                   declarations(str(member.get('declaration') or '')))


def finding(rule, obj, area, line, message, confidence='medium'):
    return dict(rule='TCSA' + rule, inspired_by='SA' + rule, severity='warning',
                object=str(obj.get('name') or '<unnamed>'), area=area, line=line, column=1,
                message=message, confidence=confidence)


def uses(code, name):
    """Bare identifier use, excluding qualified fields and named port labels."""
    clean = mask(code)
    for match in re.finditer(r'\b' + re.escape(name) + r'\b', clean, re.I):
        if clean[:match.start()].rstrip().endswith('.'):
            continue
        before = clean[:match.start()]
        if before.count('(') > before.count(')') and clean[match.end():].lstrip().startswith((':=','=>')):
            continue
        yield match


def project_checks(objects, project_wide=True):
    findings, enums = [], {}
    all_areas = [(obj, area, text, local) for obj in objects for area, text, local in areas(obj)]
    global_names = {}
    for obj in objects:
        for var in declarations(str(obj.get('declaration') or '')):
            if var['scope'] == 'VAR_GLOBAL':
                global_names[var['name']] = global_names.get(var['name'], 0) + 1
    for obj in objects:
        decl = str(obj.get('declaration') or '')
        clean = mask(decl)
        # Attribute strings are masked separately from comments, so inspect
        # genuine pragma ranges in the original text at the preserved offsets.
        qualified = any(re.search(r"\battribute\s+'qualified_only'", decl[m.start():m.end()], re.I)
                        for m in re.finditer(r'\{[^{}]*\}', clean))
        if not qualified:
            for enum in re.finditer(r'\bTYPE\s+(\w+)\s*:\s*\((.*?)\)', clean, re.I | re.S):
                for entry in re.finditer(r'(?:^|,)\s*(\w+)', enum[2]):
                    name = entry[1].upper()
                    line = clean.count('\n', 0, enum.start(2) + entry.start(1)) + 1
                    if name in enums:
                        findings.append(finding('0027', obj, 'declaration', line,
                            f"Enumeration constant '{entry[1]}' is also declared in '{enums[name]}'.", 'high'))
                    else:
                        enums[name] = enum[1]
        for var in declarations(decl):
            used_in = set()
            for other, area, text, local in all_areas:
                if var['scope'] != 'VAR_GLOBAL' and other is not obj:
                    if var['scope'] == 'VAR_OUTPUT':
                        for instance in declarations(str(other.get('declaration') or '')) + local:
                            if re.fullmatch(r'(?:ARRAY\s*\[[^]]+\]\s*OF\s+)?' + re.escape(str(obj.get('name','')).upper()), instance['type']):
                                reference = r'\b' + re.escape(instance['name']) + r'(?:\s*\[[^]]+\])?\s*\.\s*' + re.escape(var['name']) + r'\b'
                                if re.search(reference,mask(text),re.I): used_in.add(id(other))
                    continue
                shadows = {v['name'] for v in local}
                if var['scope'] == 'VAR_GLOBAL':
                    full = re.search(r'\b' + re.escape(str(obj.get('name', ''))) + r'\s*\.\s*' + re.escape(var['name']) + r'\b', mask(text), re.I)
                    parent = {v['name'] for v in declarations(str(other.get('declaration') or ''))}
                    bare = var['name'] not in shadows | parent and any(uses(text, var['name']))
                    if full or bare:
                        used_in.add(id(other))
                elif var['name'] not in shadows and any(uses(text, var['name'])):
                    used_in.add(id(other))
            # Initializer dependencies also constitute uses, but a variable's
            # own initial value does not count as a use of that variable.
            initial_use = any(any(uses(v['initial'], var['name']))
                              for v in declarations(decl) if v['name'] != var['name'])
            if var['scope'] == 'VAR_GLOBAL' and project_wide and len(used_in) == 1:
                findings.append(finding('0043', obj, 'declaration', var['line'],
                    f"Global variable '{var['display']}' is used in only one supplied POU; check HMI/ADS consumers before localizing it."))
            eligible = var['scope'] in {'VAR', 'VAR_TEMP', 'VAR_OUTPUT', 'VAR_GLOBAL'}
            if var['scope'] == 'VAR_GLOBAL' and not project_wide:
                eligible = False
            if eligible and not used_in and not initial_use:
                findings.append(finding('0033', obj, 'declaration', var['line'],
                    f"Variable '{var['display']}' has no use in the supplied source; external consumers are not resolved."))
        outputs = {v['name'] for v in declarations(decl) if v['scope'] == 'VAR_OUTPUT'}
        for area, text, local in areas(obj):
            active = outputs - {v['name'] for v in local}
            active |= {v['name'] for v in local if v['scope'] == 'VAR_OUTPUT'}
            clean_code = mask(text)
            for name in active:
                for use in uses(text, name):
                    tail = clean_code[use.end():].lstrip()
                    if tail.startswith((':=', '=>')):
                        continue
                    # Output indexed on an assignment's LHS is a write.
                    if re.match(r'^\[[^\]]*\]\s*:=', tail):
                        continue
                    if re.match(r'^(?:\.\s*\w+\s*)+:=', tail):
                        continue
                    findings.append(finding('0038', obj, area,
                        clean_code.count('\n', 0, use.start()) + 1,
                        f"Read access to own output variable '{name}'; use an internal working variable.", 'high'))
    return findings


def cognitive_complexity(text):
    """Token-based implementation of the documented increments (ST subset)."""
    score, stack, chain = 0, [], ''
    for tok in tokens(text):
        word = tok.value
        if word in {'IF', 'FOR', 'WHILE', 'REPEAT', 'CASE'}:
            score += 1 + len(stack)
            stack.append(word)
            chain = ''
        elif word.startswith('END_'):
            if stack and word == 'END_' + stack[-1]:
                stack.pop()
            chain = ''
        elif word in {'ELSIF', 'ELSE'}:
            score += 1
            chain = ''
        elif word in {'AND', 'OR', 'XOR', 'AND_THEN', 'OR_ELSE'}:
            if chain != word:
                score += 1
            chain = word
        elif word in {'SEL', 'MUX', 'JMP'}:
            score += 1
        elif word in {';', ':=', ',', 'THEN', 'DO', 'OF', 'UNTIL'}:
            chain = ''
    return score


# Intervals are deliberately bounded to supported expressions. Unknown calls,
# dereferences and unsupported casts never acquire a fabricated constant value.
UNKNOWN = (-math.inf, math.inf)
_TYPES = {'SINT':(-128,127), 'USINT':(0,255), 'INT':(-32768,32767), 'UINT':(0,65535),
          'DINT':(-2147483648,2147483647), 'UDINT':(0,4294967295), 'BOOL':(0,1)}


def join(a, b):
    return min(a[0], b[0]), max(a[1], b[1])


def expression(ts, env):
    """A small Pratt evaluator; consumes the whole expression or returns unknown."""
    values = [t.value for t in ts]
    pos = 0
    prec = {'OR':1, 'XOR':1, 'AND':2, '=':3, '<>':3, '>':3, '<':3, '>=':3, '<=':3,
            '+':4, '-':4, '*':5, '/':5, 'MOD':5}

    def calculate(op, a, b):
        if op in {'=', '<>', '>', '<', '>=', '<='}:
            truth = {'>':a[0]>b[1], '<':a[1]<b[0], '>=':a[0]>=b[1], '<=':a[1]<=b[0],
                     '=':a[0]==a[1]==b[0]==b[1], '<>':a[1]<b[0] or b[1]<a[0]}[op]
            false = {'>':a[1]<=b[0], '<':a[0]>=b[1], '>=':a[1]<b[0], '<=':a[0]>b[1],
                     '=':a[1]<b[0] or b[1]<a[0], '<>':a[0]==a[1]==b[0]==b[1]}[op]
            return (1,1) if truth else (0,0) if false else (0,1)
        if op == 'AND':
            return (0,0) if a == (0,0) or b == (0,0) else (1,1) if a == b == (1,1) else (0,1)
        if op == 'OR':
            return (1,1) if a == (1,1) or b == (1,1) else (0,0) if a == b == (0,0) else (0,1)
        if not all(math.isfinite(x) for x in (*a,*b)):
            return UNKNOWN
        if op == '+': return a[0]+b[0], a[1]+b[1]
        if op == '-': return a[0]-b[1], a[1]-b[0]
        if op == '*':
            products = [x*y for x in a for y in b]
            return min(products), max(products)
        return UNKNOWN

    def parse(min_prec=0):
        nonlocal pos
        if pos >= len(values): return UNKNOWN
        word = values[pos]; pos += 1
        if word in {'-', '+', 'NOT'}:
            a = parse(6)
            left = (-a[1],-a[0]) if word == '-' else (1-a[1],1-a[0]) if word == 'NOT' and a in {(0,0),(1,1),(0,1)} else a if word == '+' else UNKNOWN
        elif word == '(':
            left = parse()
            if pos >= len(values) or values[pos] != ')': return UNKNOWN
            pos += 1
        elif re.fullmatch(r'\d+(?:\.\d+)?(?:E[+-]?\d+)?', word):
            number = float(word) if '.' in word or 'E' in word else int(word)
            left = (number,number)
        elif '#' in word:
            parts=word.replace('_','').split('#')
            if parts[0].isalpha(): parts=parts[1:]
            number=int(parts[1],int(parts[0])) if len(parts)==2 else float(parts[0]) if '.' in parts[0] or 'E' in parts[0] else int(parts[0])
            left=(number,number)
        elif word in {'TRUE','FALSE'}:
            left = (1,1) if word == 'TRUE' else (0,0)
        else:
            while pos+1 < len(values) and values[pos] == '.':
                word += '.' + values[pos+1]; pos += 2
            left = env.get(word, UNKNOWN)
            if pos < len(values) and values[pos] == '(':
                pos += 1; args = []
                while pos < len(values) and values[pos] != ')':
                    before = pos; args.append(parse())
                    if pos < len(values) and values[pos] == ',': pos += 1
                    elif pos == before or pos >= len(values) or values[pos] != ')': return UNKNOWN
                if pos >= len(values): return UNKNOWN
                pos += 1; left = UNKNOWN
                if word == 'MAX' and args: left = max(a[0] for a in args), max(a[1] for a in args)
                elif word == 'MIN' and args: left = min(a[0] for a in args), min(a[1] for a in args)
                elif word.endswith(('_TO_REAL', '_TO_LREAL')) and len(args) == 1: left = args[0]
        while pos < len(values) and values[pos] in prec and prec[values[pos]] >= min_prec:
            op = values[pos]; pos += 1
            left = calculate(op, left, parse(prec[op]+1))
        return left

    try:
        result = parse()
        return result if pos == len(values) and not any(math.isnan(n) for n in result) else UNKNOWN
    except (ValueError, OverflowError, RecursionError):
        return UNKNOWN


def flow_checks(obj, globals_env):
    findings = []
    parent = declarations(str(obj.get('declaration') or ''))
    for area, text, local in areas(obj):
        declared = {v['name']:v for v in parent + local}
        initial = dict(globals_env)
        arrays = {}
        for name, var in declared.items():
            initial[name] = _TYPES.get(var['type'], UNKNOWN)
            if var['constant']:
                initial[name] = expression(tokens(var['initial']), initial)
            arr = re.match(r'ARRAY\s*\[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\]', var['type'])
            if arr: arrays[name] = (int(arr[1]), int(arr[2]))
        ts = tokens(text)
        reported = set()

        def emit(rule, tok, message):
            key = (rule,tok.offset)
            if key not in reported:
                reported.add(key)
                findings.append(finding(rule,obj,area,text.count('\n',0,tok.offset)+1,message))

        def inspect(part, env):
            for i,tok in enumerate(part):
                if tok.value in {'/', 'MOD'} and i+1 < len(part):
                    operand=i+1
                    while operand<len(part) and part[operand].value in {'+','-'}: operand+=1
                    end=operand+1
                    if operand<len(part) and part[operand].value == '(':
                        depth=1
                        while end<len(part) and depth:
                            depth += (part[end].value=='(')-(part[end].value==')'); end+=1
                    else:
                        while end+1<len(part) and part[end].value=='.': end+=2
                        if end<len(part) and part[end].value=='(':
                            depth=1; end+=1
                            while end<len(part) and depth:
                                depth += (part[end].value=='(')-(part[end].value==')'); end+=1
                    divisor = expression(part[i+1:end],env)
                    operand_text=''.join(t.value for t in part[i+1:end]).strip('()')
                    direct = re.fullmatch(r'(?:\b(?:SINT|USINT|INT|UINT|DINT|UDINT)_TO_L?REAL\()?([A-Z_]\w*(?:\.\w+)*)\)?', operand_text)
                    nonzero = direct and env.get('__NONZERO__'+direct[1]) == (1,1)
                    literal = re.fullmatch(r'\(?[+-]?\d+(?:\.\d+)?(?:E[+-]?\d+)?\)?', ''.join(t.value for t in part[i+1:end]))
                    if divisor[0] <= 0 <= divisor[1] and not literal and not nonzero:
                        emit('0040',tok,'Divisor may be zero; the supported source analysis cannot establish a nonzero value here.')
                if tok.value in arrays and i+1<len(part) and part[i+1].value=='[':
                    end=i+2
                    while end<len(part) and part[end].value!=']': end+=1
                    index=expression(part[i+2:end],env); bounds=arrays[tok.value]
                    if index[0]<bounds[0] or index[1]>bounds[1]:
                        emit('0172',tok,f'Possible array bounds violation: {tok.value} range {bounds[0]}..{bounds[1]}, inferred index {index[0]}..{index[1]}.')

        def narrow(cond, env, truth):
            out=dict(env)
            depth=0
            for i,token in enumerate(cond):
                depth += (token.value=='(')-(token.value==')')
                if depth==0 and token.value==('AND' if truth else 'OR'):
                    return narrow(cond[i+1:],narrow(cond[:i],out,truth),truth)
            # Only narrow a simple scalar comparison with a numeric constant.
            value=''.join(t.value for t in cond).strip('()')
            match=re.fullmatch(r'([A-Z_]\w*(?:\.\w+)*)(>=|<=|<>|>|<|=)(-?\d+)',value)
            if match:
                name,op,number=match.groups(); number=int(number)
                if not truth: op={'>':'<=','<':'>=','>=':'<','<=':'>','=':'<>','<>':'='}[op]
                lo,hi=out.get(name,UNKNOWN)
                integer = declared.get(name,{}).get('type') in _TYPES
                if op=='>': lo=max(lo,number+1 if integer else math.nextafter(number, math.inf))
                elif op=='<': hi=min(hi,number-1 if integer else math.nextafter(number,-math.inf))
                elif op=='>=': lo=max(lo,number)
                elif op=='<=': hi=min(hi,number)
                elif op=='=': lo=hi=number
                elif op=='<>' and number==0: out['__NONZERO__'+name]=(1,1)
                if lo<=hi: out[name]=(lo,hi)
            return out

        def block(pos, env, stop=()):
            env=dict(env)
            while pos<len(ts) and ts[pos].value not in stop:
                word=ts[pos].value
                if word in {'IF','ELSIF'}:
                    remaining=dict(env); branches=[]
                    while pos<len(ts) and ts[pos].value in {'IF','ELSIF'}:
                        start=pos; pos+=1; begin=pos
                        while pos<len(ts) and ts[pos].value!='THEN': pos+=1
                        cond=ts[begin:pos]; inspect(cond,remaining); val=expression(cond,remaining)
                        if val in {(0,0),(1,1)}:
                            emit('0062',ts[start],f'Condition is always {"TRUE" if val==(1,1) else "FALSE"} in the supported value model.')
                        pos,branch=block(pos+1,narrow(cond,remaining,True),('ELSE','ELSIF','END_IF'))
                        branches.append(branch)
                        remaining=narrow(cond,remaining,False)
                    if pos<len(ts) and ts[pos].value=='ELSE': pos,remaining=block(pos+1,remaining,('END_IF',))
                    branches.append(remaining)
                    env={k:(min(b.get(k,UNKNOWN)[0] for b in branches),max(b.get(k,UNKNOWN)[1] for b in branches))
                         for k in set().union(*branches)}
                    if pos<len(ts) and ts[pos].value=='END_IF': pos+=1
                elif word in {'FOR','WHILE','REPEAT','CASE'}:
                    # Invalidate mutable values before repeated/multi-way bodies;
                    # assignments from a preceding branch must not leak in.
                    fresh=dict(initial)
                    start=pos; pos+=1
                    terminator={'FOR':'DO','WHILE':'DO','CASE':'OF','REPEAT':''}[word]
                    begin=pos
                    while terminator and pos<len(ts) and ts[pos].value!=terminator: pos+=1
                    header=ts[begin:pos]; inspect(header,env)
                    if word=='WHILE':
                        val=expression(header,fresh)
                        if val in {(0,0),(1,1)}:
                            emit('0062',ts[start],f'Loop condition is constant in the supported source model: {bool(val[0])}.')
                    if word=='FOR':
                        values=[t.value for t in header]
                        if ':=' in values and 'TO' in values:
                            split=values.index('TO'); end=values.index('BY') if 'BY' in values else len(values)
                            lo=expression(header[2:split],env); hi=expression(header[split+1:end],env)
                            fresh[values[0]]=join(lo,hi)
                    pos,body=block(pos+(1 if terminator else 0),fresh,('END_'+word,))
                    env={k:join(initial.get(k,UNKNOWN),body.get(k,UNKNOWN)) for k in set(initial)|set(body)}
                    if pos<len(ts): pos+=1
                else:
                    # CASE labels are not expressions and begin independent paths.
                    label_end=pos
                    while label_end<len(ts) and ts[label_end].value not in {':',':=',';','IF','CASE','FOR','WHILE','REPEAT','ELSE','END_CASE'}:
                        label_end+=1
                    if label_end<len(ts) and ts[label_end].value==':':
                        pos=label_end+1; env=dict(initial); continue
                    begin=pos; depth=0
                    while pos<len(ts):
                        v=ts[pos].value
                        if depth==0 and (v==';' or v in {'ELSE','ELSIF','END_IF','END_FOR','END_WHILE','END_CASE','END_REPEAT'}): break
                        if depth==0 and pos>begin and v in {'IF','CASE','FOR','WHILE','REPEAT'}: break
                        depth += (v=='(')-(v==')'); pos+=1
                    part=ts[begin:pos]; inspect(part,env)
                    values=[t.value for t in part]
                    if ':=' in values:
                        split=values.index(':='); lhs=''.join(values[:split])
                        if re.fullmatch(r'[A-Z_]\w*(?:\.\w+)*',lhs):
                            env.pop('__NONZERO__'+lhs,None)
                            value=expression(part[split+1:],env)
                            # Reject integer ranges outside the declared type.
                            limits=_TYPES.get(declared.get(lhs,{}).get('type'),UNKNOWN)
                            env[lhs]=value if limits[0]<=value[0]<=value[1]<=limits[1] else limits
                            if any(t in {'AND','OR','XOR'} for t in values[split+1:]) and value in {(0,0),(1,1)}:
                                emit('0062',part[split+1],f'Boolean expression is constant in the supported source model: {bool(value[0])}.')
                    # Unknown call statements may modify VAR_IN_OUT arguments
                    # or shared state. Do not reuse preceding value proofs.
                    if ':=' not in values and '(' in values and values and re.match(r'^[A-Z_]',values[0]):
                        env=dict(initial)
                    if pos<len(ts) and ts[pos].value==';': pos+=1
                    elif pos==begin: pos+=1
            return pos,env
        try:
            block(0,initial)
        except RecursionError:
            # Already collected findings remain useful, but do not invent a pass.
            findings.append(finding('LIMIT',obj,area,1,'Analysis nesting limit reached; manual review required.'))
    return findings


def semantic_checks(objects, project_wide=True):
    env={}
    for obj in objects:
        for var in declarations(str(obj.get('declaration') or '')):
            if var['scope']=='VAR_GLOBAL':
                value=_TYPES.get(var['type'],UNKNOWN)
                if var['constant']: value=expression(tokens(var['initial']),env)
                env[str(obj.get('name','')).upper()+'.'+var['name']]=value
    return project_checks(objects,project_wide) + [f for obj in objects for f in flow_checks(obj,env)]
