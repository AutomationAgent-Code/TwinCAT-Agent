from types import SimpleNamespace
import pytest
from tc_template.compiler_settings import inspect_settings,parse_defines,read_settings
from tc_template.plc_write_context import review_dependencies


PATH='TIPC^PLC^Project'
def xml(fields='<ReplaceConstants>false</ReplaceConstants><CompilerDefines/><UTF8Encoding>false</UTF8Encoding>'):
    return '<TreeItem><PathName>'+PATH+'</PathName><IECProjectDef><CompilerSettings>'+fields+'</CompilerSettings></IECProjectDef></TreeItem>'


def test_observed_settings_do_not_claim_global_or_version_completeness():
    result=inspect_settings(xml(),PATH)
    assert result['replace_constants'] is False
    assert result['project_defines']=={}
    assert not result['effective_defines_complete']
    assert result['compiler_version'] is None


def test_define_values_preserve_commas():
    assert parse_defines("A, B := 'hello, world'")=={'A':None,'B':'hello, world'}


@pytest.mark.parametrize('value',['A,','A,A',"A := 'unterminated",'A := 2','A AND B'])
def test_unsupported_defines_are_unknown(value):
    with pytest.raises(ValueError): parse_defines(value)


def test_settings_missing_and_duplicate_not_false():
    assert inspect_settings(xml(''),PATH)['replace_constants'] is None
    with pytest.raises(ValueError):inspect_settings(xml('<ReplaceConstants>false</ReplaceConstants>'*2),PATH)
    with pytest.raises(ValueError):inspect_settings(xml(),PATH+'Other')


def test_read_requires_stable_settings():
    responses=iter([xml(),xml('<ReplaceConstants>true</ReplaceConstants>')])
    item=SimpleNamespace(ProduceXml=lambda recursive:next(responses))
    with pytest.raises(ValueError,match='changed'):
        read_settings(SimpleNamespace(Solution=SimpleNamespace(FullName='test.sln')),item,PATH)


@pytest.mark.parametrize('replace,approved',[(False,True),(True,False),(None,False)])
def test_lazy_settings_used_once_for_by_reference_constants(replace,approved):
    calls=[]
    def call(verb,**args):
        if verb=='compiler-settings':
            calls.append(verb)
            return {'status':'read','path':PATH,'source':'live_compiler_settings_xml','replace_constants':replace}
        if verb=='find-pou':
            return {'total':1,'matches':[{'name':'F_Read','path':PATH+'^POUs^F_Read'}]}
        return {'declaration':'FUNCTION F_Read:INT\nVAR_IN_OUT CONSTANT value:INT;END_VAR'}
    result=review_dependencies({'declaration':'PROGRAM MAIN\nVAR CONSTANT c:INT:=1;END_VAR\nVAR x:INT;END_VAR',
                               'implementation':'x:=F_Read(c);x:=F_Read(c);'},PATH+'^POUs^MAIN',call,semantic=True)
    assert result['semantic_review']['approved'] is approved,result
    assert calls==['compiler-settings']


def test_context_wrong_project_not_used():
    def call(verb,**args):
        if verb=='compiler-settings':
            return {'status':'read','path':'TIPC^Other^Project','source':'live_compiler_settings_xml','replace_constants':False}
        if verb=='find-pou': return {'total':1,'matches':[{'name':'F_Read','path':PATH+'^POUs^F_Read'}]}
        return {'declaration':'FUNCTION F_Read:INT\nVAR_IN_OUT CONSTANT value:INT;END_VAR'}
    result=review_dependencies({'declaration':'PROGRAM MAIN\nVAR CONSTANT c:INT:=1;END_VAR\nVAR x:INT;END_VAR',
        'implementation':'x:=F_Read(c);'},PATH+'^POUs^MAIN',call,semantic=True)
    assert not result['semantic_review']['approved']
    assert result['compiler_settings'] is None
