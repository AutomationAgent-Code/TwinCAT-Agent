import json
from pathlib import Path

import pytest

from tc_agent import coding_profile
from tc_template.fb_scaffold import generate_fb
from tc_template.lint import lint_objects


def test_project_profile_merges_saved_rules(tmp_path: Path):
    solution = tmp_path / "Demo.sln"
    profile_file = tmp_path / ".TwinCATAgent" / "coding_profile.json"
    profile_file.parent.mkdir()
    profile_file.write_text(json.dumps({
        "author": "Ethan",
        "extra_rules": ["所有轴位置使用 LREAL，单位 mm"],
    }, ensure_ascii=False), encoding="utf-8")
    profile = coding_profile.load_profile(str(solution))
    assert profile["author"] == "Ethan"
    assert profile["require_case_state_machine"] is True
    assert profile["extra_rules"] == ["所有轴位置使用 LREAL，单位 mm"]


def test_save_profile_rejects_unknown_fields(tmp_path: Path):
    with pytest.raises(ValueError, match="未知 coding profile"):
        coding_profile.save_profile(str(tmp_path / "Demo.sln"), {"surprise": True})


def test_prompt_contract_is_compact_and_generation_first(tmp_path: Path):
    prompt = coding_profile.prompt_contract(str(tmp_path / "Demo.sln"))
    assert "PLC 生成前硬约束" in prompt
    assert "VAR_INPUT → VAR_OUTPUT → VAR_IN_OUT → VAR → VAR CONSTANT" in prompt
    assert "不得先自由生成再依赖审核返工" in prompt


def test_service_scaffold_obeys_service_contract_without_transaction_quad():
    generated = generate_fb("Heartbeat", purpose="发布周期心跳", mode="service")
    declaration = generated["fb_declaration"]
    assert "FUNCTION_BLOCK FB_Heartbeat" in declaration
    assert "bEnable" in declaration
    assert "bDone" not in declaration
    findings = lint_objects([{
        "name": generated["fb_name"], "folder": "POUs",
        "declaration": declaration,
        "implementation": generated["fb_implementation"], "methods": [],
    }])
    assert not {"fb-status-quad", "transaction-state", "transaction-timeout"}.intersection(
        {item["rule"] for item in findings}
    )
