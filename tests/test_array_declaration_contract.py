import pytest
from tc_template.plc_syntax import syntax_findings


@pytest.mark.parametrize('scope,spec,rule', [
    ('VAR','ARRAY[3..1] OF INT','syntax-array-bounds'),
    ('VAR','ARRAY[0..2147483648] OF INT','syntax-array-bounds'),
    ('VAR','ARRAY[0..16#80000000] OF INT','syntax-array-bounds'),
    ('VAR','ARRAY[*] OF INT','syntax-open-array-scope'),
    ('VAR_IN_OUT','ARRAY[*,0..1] OF INT','syntax-open-array-dimensions'),
    ('VAR','ARRAY[0..1] OF BIT','syntax-array-bit'),
    ('VAR','POINTER TO BIT','syntax-reference-bit'),
    ('VAR','REFERENCE TO BIT','syntax-reference-bit')])
def test_invalid_array_contract(scope,spec,rule):
    assert rule in {f['rule'] for f in syntax_findings({'declaration':f'{scope} a:{spec}; END_VAR'})}


@pytest.mark.parametrize('scope,spec', [('VAR','ARRAY[-2..0,1..3] OF INT'),
    ('VAR','ARRAY[cMin..cMax] OF INT'),('VAR_IN_OUT','ARRAY[*,*] OF INT'),
    ('VAR','ARRAY[0..16#FF] OF BYTE')])
def test_no_rejection_of_supported_or_symbolic_bounds(scope,spec):
    assert not syntax_findings({'declaration':f'{scope} a:{spec}; END_VAR'})
