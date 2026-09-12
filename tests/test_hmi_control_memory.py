from unittest.mock import patch
import pytest
from tc_template.hmi_contract import control_schema, HmiContractError
from tc_agent.agent_core import tool_result_for_context


def test_schema_context_preserves_all_names_under_small_budget():
    attrs = [{'name': 'data-tchmi-property-' + str(i), 'description': 'x' * 500} for i in range(100)]
    result = tool_result_for_context('tc_hmi_control_schema', {'control_type': 'T'},
        {'framework': 'native1.12-tchmi', 'attributes': attrs}, limit=1000)
    assert result['attribute_names'] == [a['name'] for a in attrs]
    assert result['attribute_count'] == 100


@pytest.mark.parametrize('name,attribute,code', [('Missing', '', 'unknown_control_type'),
    ('Textblock', 'data-tchmi-font-size', 'unknown_attribute')])
def test_schema_errors_provide_real_candidates(name, attribute, code):
    with patch('tc_template._ps_bridge.ps_com', return_value={'project_file': 'fixture'}), \
         patch('tc_template.hmi_contract.Catalog') as cls:
        catalog = cls.return_value
        catalog.controls = {'Textblock': {}}
        catalog.attributes.return_value = {'data-tchmi-text-font-size': {}}
        with pytest.raises(HmiContractError) as err:
            control_schema(control_type=name, attribute=attribute)
    assert err.value.details['code'] == code
    assert err.value.details['retry_safe'] is False
    if attribute:
        assert err.value.details['candidates'] == ['data-tchmi-text-font-size']
