"""Observed live IECProjectDef/CompilerSettings, not saved-file or runtime guesses."""
import hashlib
import json
import re
from xml.etree import ElementTree as ET


def parse_defines(text):
    if not isinstance(text,str) or len(text)>65536:
        raise ValueError('Compiler defines exceed budget')
    result={}
    position=0
    while position<len(text):
        if not text[position:].strip(): break
        match=re.match(r"\s*([A-Za-z_]\w*)\s*(?::=\s*'([^'$]*)'\s*)?(,|$)",text[position:])
        if not match: raise ValueError('Unsupported compiler define expression')
        if match[1] in result: raise ValueError('Duplicate compiler definition')
        result[match[1]]=match[2]
        position+=match.end()
        if len(result)>4096: raise ValueError('Compiler definition count exceeds budget')
        if match[3]==',' and not text[position:].strip(): raise ValueError('Trailing compiler define separator')
    return result


def inspect_settings(xml,path):
    if not isinstance(xml,str) or len(xml)>2_000_000 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)',xml,re.I):
        raise ValueError('Unsupported compiler settings XML')
    root=ET.fromstring(xml)
    if root.tag!='TreeItem' or root.findtext('PathName')!=path:
        raise ValueError('Compiler settings path does not match the bound PLC project')
    settings=root.findall('./IECProjectDef/CompilerSettings')
    if len(settings)!=1: raise ValueError('Compiler settings are missing or ambiguous')
    settings=settings[0]
    def text(name):
        fields=settings.findall(name)
        if len(fields)>1: raise ValueError('Duplicate compiler setting: '+name)
        if fields and list(fields[0]): raise ValueError('Unexpected nested compiler setting')
        return (fields[0].text or '') if fields else None
    def boolean(name):
        value=text(name)
        if value is None: return None
        if value.strip() not in {'true','false','0','1'}: raise ValueError('Invalid boolean compiler setting')
        return value.strip() in {'true','1'}
    raw=text('CompilerDefines')
    try:
        defines=parse_defines(raw) if raw is not None else None
        reason='' if defines is not None else 'Project compiler definitions not exposed'
    except ValueError as exc:
        defines=None
        reason=str(exc)
    result={'status':'read','path':path,'source':'live_compiler_settings_xml',
            'project_defines':defines,'project_defines_complete':defines is not None,
            'system_defines_complete':False,'effective_defines_complete':False,
            'defines_reason':reason or 'System/variant compiler definitions are not exposed by this XML subset',
            'replace_constants':boolean('ReplaceConstants'),'utf8_encoding':boolean('UTF8Encoding'),
            'compiler_version':None,'compiler_verified':False}
    result['sha256']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def read_settings(dte,item,path):
    solution=str(dte.Solution.FullName or '')
    before=inspect_settings(str(item.ProduceXml(False)),path)
    after=inspect_settings(str(item.ProduceXml(False)),path)
    if not solution or solution!=str(dte.Solution.FullName or '') or before!=after:
        raise ValueError('Compiler settings or solution changed during read')
    return {**after,'solution':solution}
