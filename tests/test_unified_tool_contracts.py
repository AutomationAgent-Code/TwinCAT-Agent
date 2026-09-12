import ast
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tc_agent import agent_core as ac, backend
from tc_agent.tool_contract_inventory import CONTRACT_TOOLS
from tc_agent.tool_contracts import compile_contract, selected_contract_prompt


def test_all_registered_tools_have_one_versioned_contract():
    assert CONTRACT_TOOLS == {tool["name"] for tool in ac.REGISTRY}
    for tool in ac.REGISTRY:
        c = tool["contract"]
        assert c["tool"] == tool["name"] and c["version"] == 1
        assert tool["preconditions"] is c["preconditions"]
        assert c["required_parameters"] == tool["parameters"].get("required", [])
        for field in ("applicability", "parameter_source", "preconditions", "approval", "side_effect", "verification", "recovery"):
            assert c[field]
        assert all(p["enforcement"] for p in c["preconditions"])
        assert ac.tool_metadata(tool["name"])["contract"] == c


def test_new_tool_requires_explicit_inventory_review():
    with pytest.raises(ValueError, match="inventory"):
        compile_contract({"name": "new_unreviewed_tool", "description": "test", "parameters": {"type": "object"}})


def test_model_only_receives_selected_contracts_and_deduplicates():
    schemas = ac.tools_schema(allowed_names={"plc_write", "plc_patch"})
    prompt = selected_contract_prompt(schemas, ac.tool_metadata)
    profile = ac.tool_metadata("plc_write")["contract"]["profile_id"]
    assert prompt.count(f"[{profile}]") == 1
    assert "plc_write" in prompt and "plc_patch" in prompt
    assert "tc_hmi_control_edit" not in prompt
    assert "source_editor_state" in prompt and "检查:" in prompt
    assert all(ac.tool_metadata(schema['name'])['contract']['profile_id'] in schema['description']
               for schema in schemas)
    assert {'plc_read', 'plc_diagnostics', 'plc_build_status'} <= {s['name'] for s in schemas}
    assert selected_contract_prompt(schemas, ac.tool_metadata) == prompt
    assert selected_contract_prompt([], ac.tool_metadata) == ""


def test_missing_runtime_contract_blocks_before_com():
    handler = Mock()
    with patch.dict(ac._BY_NAME["plc_write"], {"contract": None, "run": handler}), \
         patch.object(ac, "ps_com", side_effect=AssertionError("must not query XAE")):
        result = ac.run_tool("plc_write", {"name": "MAIN", "area": "implementation", "code": ""})
    assert result["not_executed"] and result["condition"] == "tool_contract"
    handler.assert_not_called()


def test_all_model_entrypoints_rebuild_contracts_outside_history():
    # Check the real request boundaries, not a duplicate helper implementation.
    tree = ast.parse(Path(backend.__file__).read_text(encoding="utf-8-sig"))
    for name in ("stream_complete", "complete_worker_step"):
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "selected_contract_prompt" for n in ast.walk(node))
    source = Path(ac.__file__).read_text(encoding="utf-8-sig")
    assert "SYSTEM_PROMPT + selected_contract_prompt(schema, tool_metadata)" in source


def test_external_contract_does_not_trust_readonly_annotation():
    c = compile_contract({"name": "mcp_remote_read", "readonly": True, "description": "",
                          "parameters": {"type": "object"}}, external=True)
    assert c["side_effect"] != "none"
    assert "逐次人工审批" in c["approval"] and "不可信" in c["parameter_source"]


def test_metadata_return_cannot_mutate_executor_contract():
    metadata = ac.tool_metadata("plc_write")
    metadata["contract"]["preconditions"].clear()
    assert ac._BY_NAME["plc_write"]["contract"]["preconditions"]


def test_selected_tool_without_contract_is_not_silently_omitted():
    with pytest.raises(ValueError, match="missing contract"):
        selected_contract_prompt([{"name": "unknown"}], lambda _: {})
