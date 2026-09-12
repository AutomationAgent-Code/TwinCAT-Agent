from tc_template.static_analysis import analyze_objects
from tc_template.static_rules import cognitive_complexity, expression, tokens


def obj(name='MAIN', decl='', code='', **extra):
    return dict(name=name, declaration=decl, implementation=code, **extra)


def matches(objects, rule, **kwargs):
    return [f for f in analyze_objects(objects, **kwargs)['findings'] if f.get('inspired_by') == rule]


def test_own_output_read_in_body_and_method_but_not_external_output_or_port_name():
    x=obj('FB_Test','FUNCTION_BLOCK FB_Test\nVAR_OUTPUT\n nOut : INT;\nEND_VAR',
          'nOut := 1;\nx := other.nOut;\nfb(nOut := 3);\nIF nOut > 0 THEN x := 1; END_IF;',
          methods=[dict(name='Get',declaration='METHOD Get',implementation='x := nOut;'),
                   dict(name='Shadow',declaration='METHOD Shadow\nVAR\n nOut : INT;\nEND_VAR',implementation='x := nOut;')])
    found=matches([x],'SA0038')
    assert {(f['area'],f['line']) for f in found}=={('implementation',4),('member:Get',1)}


def test_enum_qualified_only_exemption_and_strict_is_not_exemption():
    a=obj('E_A',"{attribute 'strict'}\nTYPE E_A : (Running, Fault); END_TYPE")
    b=obj('E_B','TYPE E_B : (Running, Fault); END_TYPE')
    assert len(matches([a,b],'SA0027'))==2
    a['declaration']="{attribute 'qualified_only'}\n"+a['declaration']
    assert not matches([a,b],'SA0027')
    a['declaration']="(* {attribute 'qualified_only'} *)\nTYPE E_A : (Running, Fault); END_TYPE"
    assert len(matches([a,b],'SA0027'))==2


def test_unused_outputs_constants_globals_and_single_pou_globals():
    g=obj('GVL_Hmi','VAR_GLOBAL\n nOnly : INT;\n nBoth : INT;\n nUnused : INT;\nEND_VAR')
    a=obj('A','PROGRAM A\nVAR_OUTPUT\n bError : BOOL;\nEND_VAR\nVAR CONSTANT\n C_UNUSED : INT := 5;\nEND_VAR',
          'GVL_Hmi.nOnly := 1; GVL_Hmi.nBoth := 2;')
    b=obj('B','PROGRAM B','x := GVL_Hmi.nBoth;')
    assert len(matches([g,a,b],'SA0043'))==1
    assert len(matches([g,a,b],'SA0033'))==3
    assert not matches([g],'SA0043',project_wide=False)
    assert not matches([g],'SA0033',project_wide=False)


def test_method_shadow_does_not_hide_unused_parent():
    a=obj('FB_A','FUNCTION_BLOCK FB_A\nVAR\n x : INT;\nEND_VAR','',
          methods=[dict(name='M',declaration='METHOD M\nVAR\n x : INT;\nEND_VAR',implementation='x := 1;')])
    assert len(matches([a],'SA0033'))==1


def test_official_complexity_examples():
    assert cognitive_complexity('IF TRUE THEN ; END_IF WHILE TRUE DO ; END_WHILE FOR i := 0 TO 10 DO ; END_FOR REPEAT ; UNTIL TRUE END_REPEAT')==4
    assert cognitive_complexity('IF TRUE THEN WHILE TRUE DO FOR i := 0 TO 10 DO ; END_FOR END_WHILE REPEAT ; UNTIL TRUE END_REPEAT END_IF')==8
    for expression,expected in [('b1',0),('b1 AND b2',1),('b1 AND b2 AND b3',1),('b1 AND b2 OR b3',2),('b1 AND b2 OR b3 AND b4 AND b5',3),('b1 AND NOT b2 AND b3',1)]:
        assert cognitive_complexity('b := '+expression+';')==expected
    assert cognitive_complexity('label: x := MUX(i,a,b); y := SEL(b,i,j); JMP label; RETURN; EXIT;')==3


def test_flow_assigned_nonzero_and_unknown_divisor():
    a=obj(decl='PROGRAM MAIN\nVAR\n d : INT;\nEND_VAR',code='d := 100; x := y / d; x := y / unknown;')
    assert len(matches([a],'SA0040'))==1


def test_guarded_divisor_and_reassignment_invalidates_guard():
    a=obj(decl='PROGRAM MAIN\nVAR\n d : INT;\nEND_VAR',code='IF d > 0 THEN x := y / d; d := 0; x := y / d; END_IF;')
    assert len(matches([a],'SA0040'))==1
    a['implementation']='IF d <> 0 THEN x := y / DINT_TO_REAL(d); d := 0; x := y / d; END_IF;'
    assert len(matches([a],'SA0040'))==1


def test_branches_do_not_leak_nonzero_values():
    a=obj(decl='PROGRAM MAIN\nVAR\n d : INT;\nEND_VAR',code='IF b THEN d := 1; ELSIF c THEN d := 2; END_IF; x := 1 / d;')
    assert matches([a],'SA0040')


def test_array_loop_range_and_direct_unknown_index():
    decl='PROGRAM MAIN\nVAR\n a : ARRAY[0..10] OF INT;\n i : INT;\nEND_VAR'
    safe=obj(decl=decl,code='FOR i := 0 TO 10 DO a[i] := 0; END_FOR;')
    unsafe=obj(decl=decl,code='FOR i := 0 TO 50 DO a[i] := 0; END_FOR;')
    assert not matches([safe],'SA0172')
    safe['implementation']='IF i >= 0 AND i < 11 THEN a[i] := 0; END_IF;'
    assert not matches([safe],'SA0172')
    assert len(matches([unsafe],'SA0172'))==1
    safe['implementation']='a[i] := 0;'
    assert matches([safe],'SA0172')
    safe['implementation']='FOR i := INT#0 TO INT#10 DO a[i] := 0; END_FOR;'
    assert not matches([safe],'SA0172')


def test_constant_condition_after_assignment_and_unknown_call():
    a=obj(decl='PROGRAM MAIN\nVAR\n n : INT;\nEND_VAR',code='n := 0; IF n <> 0 THEN x := 1; END_IF; IF MAX(n,1) >= 1 THEN x := 2; END_IF;')
    assert len(matches([a],'SA0062'))==2
    assert expression(tokens('Unresolved(n)'),{'N':(1,1)})[0]==float('-inf')


def test_rule_configuration_and_default_threshold():
    a=obj(decl='PROGRAM MAIN\nVAR_OUTPUT\n x : INT;\nEND_VAR',code='y := x;')
    result=analyze_objects([a],rule_severities={'SA0038':'error','SA0033':'off'})
    assert result['configuration']['max_complexity']==20
    assert result['summary']['errors']==1
    assert not matches([a],'SA0038',rule_severities={'SA0038':'off'})


def test_compiled_boolean_constant_example():
    a=obj(decl='PROGRAM MAIN\nVAR\n b : BOOL;\nEND_VAR',code='b := input OR TRUE;')
    assert len(matches([a],'SA0062'))==1


def test_external_output_read_counts_as_use_of_output():
    block=obj('FB_Test','FUNCTION_BLOCK FB_Test\nVAR_OUTPUT\n nOut : INT;\nEND_VAR','')
    caller=obj('MAIN','PROGRAM MAIN\nVAR\n fb : FB_Test;\nEND_VAR','x := fb.nOut;')
    assert not matches([block,caller],'SA0033')


def test_named_port_label_does_not_count_as_local_variable_use():
    a=obj(decl='PROGRAM MAIN\nVAR\n nUnused : INT;\nEND_VAR',code='SomeFB(nUnused := 2);')
    assert len(matches([a],'SA0033'))==1


def test_unknown_call_invalidates_prior_nonzero_value():
    a=obj(decl='PROGRAM MAIN\nVAR\n d : INT;\nEND_VAR',code='d := 5; Unknown(d); x := 1 / d;')
    assert matches([a],'SA0040')


def test_cli_and_agent_forward_rule_configuration():
    from unittest.mock import patch
    from click.testing import CliRunner
    from tc_template.cli import main
    from tc_agent.agent_core import REGISTRY
    a=obj(decl='PROGRAM MAIN\nVAR_OUTPUT\n x : INT;\nEND_VAR',code='y := x;')
    with patch('tc_template._ps_bridge.com_all_code',return_value=[a]):
        result=CliRunner().invoke(main,['plc','analyze','--rule','SA0038=error'])
        assert result.exit_code!=0
        assert 'TCSA0038' in result.output
    tool=next(t for t in REGISTRY if t['name']=='plc_static_analysis')
    with patch('tc_agent.agent_core.ps_com',return_value=[a]):
        result=tool['run']({'rule_severities':{'SA0038':'error'}})
        assert result['summary']['errors']==1
