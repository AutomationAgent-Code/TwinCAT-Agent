from tc_template.plc_write_context import review_dependencies


def test_actual_namespace_and_effective_version_required():
    obj={'name':'F','library':'DifferentLibraryName','version':'1.0','source':'live_library_signatures',
         'signature_complete':True,'declaration':'FUNCTION F:DINT\nVAR_INPUT value:INT; END_VAR'}
    def call(verb,**args):
        if verb=='library-evidence':
            return {'status':'read','libraries':[{'name':'DifferentLibraryName','namespace':'Custom','effective_version':'1.0'}]}
        if verb=='library-signatures':
            return {'status':'read','libraries':[{'name':'DifferentLibraryName','version':'1.0','objects':[obj]}]}
        return {'matches':[],'total':0}
    candidate={'declaration':'PROGRAM MAIN\nVAR result:DINT; END_VAR','implementation':'result := Custom.F(1);'}
    result=review_dependencies(candidate,'TIPC^PLC^Project^POUs^MAIN',call,semantic=True)
    assert result['semantic_review']['approved'],result
    candidate['implementation']='result := DifferentLibraryName.F(1);'
    assert not review_dependencies(candidate,'TIPC^PLC^Project^POUs^MAIN',call,semantic=True)['semantic_review']['approved']
