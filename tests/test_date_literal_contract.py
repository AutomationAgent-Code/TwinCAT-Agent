import pytest
from tc_template.st_preflight import review_candidate


@pytest.mark.parametrize('typ,value,ok',[('DATE','DATE#2024-2-29',True),
    ('DATE','DATE#2025-2-29',False),('DATE','D#1969-1-1',False),
    ('TIME_OF_DAY','TOD#23:59:59.999',True),('TOD','TOD#24:0:0',False),
    ('DATE_AND_TIME','DT#2106-2-7-6:28:15',True),('DT','DT#2106-2-7-6:28:16',False)])
def test_date_bounds(typ,value,ok):
    result=review_candidate({'declaration':f'PROGRAM MAIN\nVAR stamp:{typ}; END_VAR','implementation':f'stamp := {value};'})
    assert result['approved'] is ok, result
