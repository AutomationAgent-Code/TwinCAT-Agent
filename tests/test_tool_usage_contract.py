"""The XAE incident constraints must reach CLI, foreground and worker prompts."""
from unittest.mock import patch

from tc_agent import agent_core, backend
from tc_agent.tool_usage_contract import prompt_contract


def test_contract_is_injected_in_both_prompt_entry_points():
    contract = prompt_contract()
    assert agent_core.SYSTEM_PROMPT.count(contract) == 1
    # Empty solution is also a supported context; no XAE/ADS is needed here.
    with patch.object(agent_core, 'ps_com', side_effect=AssertionError('no live probe')):
        for gate in (False, True):
            assert backend._system_prompt('', quality_gate_enabled=gate).count(contract) == 1


def test_disk_catalog_no_longer_authorizes_com_writes():
    prompt = backend._system_prompt('')
    assert '把 catalog 返回的 path 原样传给' not in prompt
    assert '不得直接传给 plc_write/plc_patch/plc_create_member' in prompt
    assert 'source=disk_index' in prompt
    assert 'plc_find' in prompt_contract()


def test_failure_and_evidence_cases_are_kept_distinct():
    text = prompt_contract()
    for signal in ('not_executed=true', 'written=true/uncertain', 'dirty_unknown=true',
                   'truncated=true', 'compiler_verified=false', 'failedProjects>0',
                   'verification_scope', 'TCSA', 'TE1200', 'repair budget/blocked',
                   'verified=false', '旧许可证消息'):
        assert signal in text
    assert '不要换工具名再次编译' in text
    assert '不靠历史摘要拼 old_text' in text
    assert '根因尚未确认' in text
