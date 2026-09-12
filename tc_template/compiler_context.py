"""Read-only compiler-context calculations. Never grants write authority."""
import re
from .interface_types import string_spec
from .type_contract import array_type, subrange, alias_target, enum_definition
from .integer_literals import integer_literal, fits_integer


def version_requirement(version, minimum='3.1.4026.0'):
    def parse(value):
        return tuple(map(int, value.split('.'))) if isinstance(value,str) and re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,8}){3}',value) else None
    actual, required = parse(version), parse(minimum)
    return {'status':'unknown' if not actual or not required else 'compatible' if actual>=required else 'incompatible',
            'actual':version, 'minimum':minimum, 'compiler_verified':False}


def source_version_requirements(candidate):
    """Feature availability only, never infer active compiler from caller JSON."""
    from .lint import _st_code
    from .conditional_compilation import preprocess_candidate
    prepared,evidence=preprocess_candidate(candidate)
    requirements=[]
    for area in ('declaration','enclosing_declaration','implementation'):
        selected=evidence.get(area,{})
        if selected.get('minimum_twincat_version'):
            requirements.append({'feature':'declaration_conditionals','area':area,
                                 **version_requirement(None,selected['minimum_twincat_version'])})
        if re.search(r'\b(?:LDATE|LDT|LTOD|LDATE_AND_TIME|LTIME_OF_DAY)\b',_st_code(prepared.get(area) or ''),re.I):
            requirements.append({'feature':'long_date_time','area':area,**version_requirement(None)})
        if re.search(r'\bXSIZEOF\s*\(', _st_code(prepared.get(area) or ''), re.I):
            requirements.append({'feature':'xsizeof','area':area,**version_requirement(None)})
    return requirements


def layout_type(spec, resolve, *, pointer_bits=None, pack=8, _seen=()):
    """Calculate fixed scalar/array/STRUCT snapshots; FB/unknown layouts fail closed.

    resolve returns an explicit {'kind':'struct','fields':[(name,type),...]} or
    {'kind':'alias','type':...} snapshot. Caller owns scope/identity resolution.
    pack is explicit for the containing structure, not inherited by nested DUTs.
    """
    def unknown(reason):
        return {'status':'unknown','reason':reason,'layout_verified':False}
    if pack not in (0,1,2,4,8) or pointer_bits not in (None,32,64):
        return unknown('Unsupported packing or target width')
    spec=spec.strip().upper()
    from .date_literals import ALIASES
    spec=ALIASES.get(spec,spec)
    if spec in _seen or len(_seen)>=24:
        return unknown('Cyclic or excessive by-value type nesting')
    limits=subrange(spec)
    if limits:
        if limits[1] is None or limits[1]>limits[2] or not all(fits_integer(n,limits[0]) for n in limits[1:]):
            return unknown('Unresolved or invalid subrange bounds')
        return layout_type(limits[0],resolve,pointer_bits=pointer_bits,_seen=(*_seen,spec))
    sizes={'BOOL':1,'SINT':1,'USINT':1,'BYTE':1,'INT':2,'UINT':2,'WORD':2,
           'DINT':4,'UDINT':4,'DWORD':4,'REAL':4,'TIME':4,'DATE':4,'DT':4,'TOD':4,
           'LINT':8,'ULINT':8,'LWORD':8,'LREAL':8,'LTIME':8,'LDATE':8,'LDT':8,'LTOD':8}
    size=sizes.get(spec)
    if spec.startswith('POINTER TO ') or spec in {'PVOID','__XINT','__UXINT','__XWORD'}:
        if pointer_bits is None:
            return unknown('Pointer width requires actual target evidence')
        size=pointer_bits//8
    if size is not None:
        return {'status':'calculated','size':size,'alignment':min(size,8),'layout_verified':False}
    text=string_spec(spec)
    if text:
        unit=2 if text[0]=='WSTRING' else 1
        return {'status':'calculated','size':(text[1]+1)*unit,'alignment':unit,'layout_verified':False}
    array=array_type(spec)
    if array:
        count=1
        for dim in array[0]:
            limits=dim.split('..')
            parsed=[integer_literal(v) for v in limits]
            if len(parsed)!=2 or any(v is None for v in parsed) or parsed[0][0]>parsed[1][0]:
                return unknown('Fixed resolved array bounds required')
            count*=parsed[1][0]-parsed[0][0]+1
        item=layout_type(array[1],resolve,pointer_bits=pointer_bits,_seen=(*_seen,spec))
        if item['status']!='calculated': return item
        return {**item,'size':item['size']*count,'element_count':count}
    obj=resolve(spec)
    if not obj: return unknown('Type snapshot missing: '+spec)
    if obj.get('kind')=='alias':
        return layout_type(obj['type'],resolve,pointer_bits=pointer_bits,_seen=(*_seen,spec))
    if obj.get('kind') not in {'struct','union'}:
        return unknown('Only explicit STRUCT/UNION layouts are supported; FBs contain implicit members')
    own_pack=obj.get('pack',pack)
    if own_pack not in (0,1,2,4,8): return unknown('Unresolved pack_mode')
    own_pack=max(1,own_pack)
    offset,alignment,fields=0,1,[]
    members=obj.get('fields',[])
    if not members or len(members)>4096: return unknown('Empty or excessive member list')
    for name,field_type in members:
        item=layout_type(field_type,resolve,pointer_bits=pointer_bits,_seen=(*_seen,spec))
        if item['status']!='calculated': return item
        field_alignment=min(item['alignment'],own_pack)
        next_offset=0 if obj['kind']=='union' else (offset+field_alignment-1)//field_alignment*field_alignment
        fields.append({'name':name,'offset':next_offset,'size':item['size'],
                       'padding_before':0 if obj['kind']=='union' else next_offset-offset})
        offset=max(offset,item['size']) if obj['kind']=='union' else next_offset+item['size']
        alignment=max(alignment,field_alignment)
    size=(offset+alignment-1)//alignment*alignment
    return {'status':'calculated','size':size,'alignment':alignment,'fields':fields,
            'tail_padding':size-offset,'layout_verified':False}


def declaration_layout(source, read_fields):
    """Extract a bounded source layout, retaining pack_mode rather than ignoring it."""
    from .lint import _st_code
    from .conditional_compilation import preprocess_editor
    selected=preprocess_editor(source,'declaration')
    if selected['status']!='processed': return None
    source=selected['source']
    code=_st_code(source)
    packs=[]
    enum_attributes=False
    first_type=re.search(r'\bTYPE\b',code,re.I)
    for match in re.finditer(r'\{[^}]*\}',code):
        raw=source[match.start():match.end()]
        if re.fullmatch(r"\{\s*attribute\s+'(?:strict|qualified_only)'\s*\}",raw,re.I) and first_type and match.end()<=first_type.start():
            enum_attributes=True
            continue
        pack=re.fullmatch(r"\{\s*attribute\s+'pack_mode'\s*:=\s*'([01248])'\s*\}",raw,re.I)
        if not pack or not first_type or match.end()>first_type.start(): return None
        packs.append(int(pack[1]))
    if len(packs)>1: return None
    code=re.sub(r'\{[^}]*\}',lambda m:re.sub(r'[^\r\n]',' ',m[0]),code)
    enum=enum_definition(code)
    if enum:
        return {'kind':'alias','type':enum['base']} if enum['status']=='parsed' and not packs else None
    if enum_attributes: return None
    alias=alias_target(code)
    if alias: return None if packs else {'kind':'alias','type':alias}
    shape=re.fullmatch(r'\s*TYPE\s+\w+\s*:\s*(STRUCT|UNION)\b(.*?)\bEND_\1\s*;?\s*END_TYPE\s*;?\s*',code,re.I|re.S)
    if not shape: return None
    fields,issues=read_fields(code)
    if issues or any(v['scope']!='FIELD' for v in fields.values()): return None
    result={'kind':shape[1].lower(),'fields':[(n,v['type']) for n,v in fields.items()]}
    if packs: result['pack']=packs[0]
    return result
