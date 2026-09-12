from pathlib import Path

from tc_agent import config
from tc_agent.backend import _system_prompt


ROOT = Path(__file__).resolve().parents[1]


def test_public_settings_exposes_code_style_profiles():
    settings = config.public_settings({
        "active_provider": "",
        "providers": [],
        "perm_mode": "auto",
        "code_style": "ham",
    })
    assert settings["code_style"] == "ham"
    assert {item["id"] for item in settings["code_styles"]} == {
        "project", "standard", "ham"
    }
    assert all("prompt" not in item for item in settings["code_styles"])


def test_invalid_saved_style_migrates_to_project_default():
    migrated = config._migrate({
        "active_provider": "",
        "providers": [{"id": "api", "kind": "api"}],
        "code_style": "unknown",
    })
    assert migrated["code_style"] == "project"


def test_ham_style_is_injected_into_agent_system_prompt():
    prompt = _system_prompt("demo.sln", "ham")
    assert "编程风格选择为【HAM】" in prompt
    assert "A_Init" in prompt
    assert "demo.sln" in prompt


def test_te1200_baseline_is_mandatory_in_agent_system_prompt():
    prompt = _system_prompt("demo.sln", "project")
    assert "TE1200" in prompt


def test_agent_prompt_requires_batch_reads_for_whole_programs():
    prompt = _system_prompt("demo.sln", "project")
    assert "禁止逐个连续调用 plc_read" in prompt
    assert "plc_read_fast" in prompt
    assert "SA0004" in prompt
    assert "编译成功不等于通过 TE1200" in prompt
    assert "未执行 TE1200 静态分析" in prompt


def test_generation_contract_and_template_first_workflow_are_injected():
    prompt = _system_prompt("demo.sln", "standard")
    assert "PLC 生成前硬约束" in prompt
    assert "fblib_find" in prompt
    assert "plc_generate" in prompt
    assert "plc_create_standard_fb" in prompt
    assert "审核只是写入前安全兜底" in prompt
    assert "agent_report_create" in prompt


def test_webview_contains_style_selector_and_message_flow():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="styleSel"' in html
    assert 'type: "set_style"' in html


def test_webview_contains_theme_selector_and_local_theme_persistence():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="themeSel"' in html
    assert 'value="system"' in html
    assert 'value="light"' in html
    assert 'value="dark"' in html
    assert 'localStorage.getItem("tc-agent-theme")' in html
    assert 'data-theme' in html


def test_language_controls_ui_and_plc_comment_language():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="languageSel"' in html
    assert 'value="en"' in html
    assert 'type: "set_language"' in html
    assert 'tc-agent-language' in html
    assert "use English for explanations and PLC comments" in _system_prompt("demo.sln", "project", "en")
    assert 'case "style_changed"' in html
