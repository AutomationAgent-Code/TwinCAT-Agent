from tc_template.member_catalog import catalog
from tc_template.plc_write_context import review_dependencies


class Item:
    def __init__(self,name='',kind=601,children=()):
        self.Name,self.ItemType,self.children=name,kind,children
    def __iter__(self): return iter(self.children)


def test_folder_inventory_and_failure():
    result=catalog(Item(children=[Item('Folder',601,[Item('Calc',609)])]))
    assert result['complete']
    assert result['entries'][0]['relative_path']=='Folder.Calc'
    class Broken:
        def __iter__(self): raise RuntimeError('busy')
    assert not catalog(Broken())['complete']
    assert not catalog(Item(children=[Item('Unknown',999)]))['complete']


def test_qualified_base_is_not_truncated():
    from tc_template.st_preflight import review_candidate
    reads=[]
    def resolve(name):
        reads.append(name)
        if name.upper()=='MOTION.FB_BASE':
            return {'declaration':'FUNCTION_BLOCK FB_Base\nVAR_INPUT nValue:INT; END_VAR'}
    result=review_candidate({'declaration':'FUNCTION_BLOCK FB_Child EXTENDS Motion.FB_Base\nVAR x:INT; END_VAR',
                             'implementation':'x := nValue;'},resolve=resolve)
    assert result['approved'],result
    assert 'Motion.FB_Base' in reads
    assert 'Motion' not in reads


def check(complete=True,own=False,private=False):
    reads=[]
    child='FUNCTION_BLOCK FB_Child EXTENDS FB_Base'
    base='FUNCTION_BLOCK FB_Base'
    def call(verb,**args):
        name=args.get('query',args.get('name','')).upper()
        if verb=='find-pou': return {'total':1,'matches':[{'name':name,'path':'TIPC^PLC^Project^POUs^'+name}]}
        if args.get('method'):
            reads.append(name)
            if name=='FB_CHILD': raise RuntimeError('editor busy')
            return {'declaration':'METHOD '+('PRIVATE ' if private else 'PUBLIC ')+'Calc:INT'}
        return {'declaration':child if name=='FB_CHILD' else base,
                'member_catalog':{'complete':complete,'entries':[{'name':'Calc','relative_path':'Calc'}] if own else []}}
    result=review_dependencies({'declaration':'PROGRAM MAIN\nVAR fb:FB_Child; x:INT; END_VAR',
                                 'implementation':'x := fb.Calc();'},'TIPC^PLC^Project^POUs^MAIN',call,semantic=True)
    return result,reads


def test_inherited_method_when_absence_proven():
    result,reads=check()
    assert result['semantic_review']['approved'],result
    assert reads==['FB_BASE']


def test_busy_own_member_never_falls_back():
    for complete,own in [(True,True),(False,False)]:
        result,reads=check(complete,own)
        assert not result['semantic_review']['approved']
        assert 'FB_BASE' not in reads


def test_private_base_method_rejected():
    result,_=check(private=True)
    assert not result['semantic_review']['approved']
