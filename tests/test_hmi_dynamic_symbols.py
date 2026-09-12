from unittest.mock import patch

import pytest

from tc_template import _ps_bridge as bridge
from tc_template.hmi_dynamic_symbols import HmiDynamicSymbolValidationError


def test_incomplete_symbol_is_rejected_before_com_with_complete_example():
    with patch.object(bridge, "_ps_com_raw") as call:
        with pytest.raises(HmiDynamicSymbolValidationError) as caught:
            bridge.ps_com("hmi-dynamic-symbols-set", symbols={
                "ADS.PLC1.GVL_Hmi.bStart": {"type": "BOOL"},
            }, definitions={}, project="HMI", apply=False)
    call.assert_not_called()
    assert set(caught.value.details["missing_fields"]) == {"DOMAIN", "DYNAMIC", "MAPPING", "USEMAPPING"}
    assert caught.value.details["complete_example"]["symbols"]
    assert "IndexGroup" in caught.value.details["note"]


@pytest.mark.parametrize("apply", [False, True])
def test_camel_case_contract_is_normalized_for_preview_and_apply(apply):
    source = {"ADS.PLC1.GVL_Hmi.bStart": {
        "access": 3, "domain": "ADS", "dynamic": True,
        "mapping": "PLC1::GVL_Hmi::bStart", "type": "BOOL", "useMapping": True,
    }}
    with patch.object(bridge, "_ps_com_raw", return_value={"status": "applied" if apply else "preview"}) as call:
        result = bridge.ps_com("hmi-dynamic-symbols-set", symbols=source,
                               definitions={}, project="HMI", apply=apply)
    assert result["status"] == ("applied" if apply else "preview")
    sent = call.call_args.kwargs["symbols"]["ADS.PLC1.GVL_Hmi.bStart"]
    assert sent["DOMAIN"] == "ADS" and sent["DYNAMIC"] is True and sent["USEMAPPING"] is True
    assert sent["SCHEMA"] == {"$ref": "tchmi:general#/definitions/BOOL"}


def test_mapping_runtime_mismatch_is_rejected():
    with pytest.raises(HmiDynamicSymbolValidationError, match="different Runtime"):
        bridge.ps_com("hmi-dynamic-symbols-set", symbols={
            "ADS.PLC1.GVL_Hmi.bStart": {
                "DOMAIN": "ADS", "DYNAMIC": True, "USEMAPPING": True,
                "MAPPING": "PLC2::GVL_Hmi::bStart",
                "SCHEMA": {"$ref": "tchmi:general#/definitions/BOOL"},
            }
        })
