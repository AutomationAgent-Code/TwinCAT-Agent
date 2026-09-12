from tc_agent.system_manager_diagnostics import (
    combine_build_and_diagnostics,
    normalize_system_manager_diagnostics,
)


def test_empty_level_ten_reply_is_incomplete_not_clean():
    result = normalize_system_manager_diagnostics({"level": 10, "messages": []}, project_id=7)
    assert result["status"] == "incomplete"
    assert result["verified"] is False
    assert result["errors"] == 0


def test_protocol_error_is_preserved_and_not_overwritten_by_success():
    result = combine_build_and_diagnostics(
        {"succeeded": True, "buildPerformed": True},
        {"protocolError": "unavailable", "messages": []},
    )
    assert result["success"] is False
    assert result["status"] == "incomplete"
    assert result["protocol_error"] == "unavailable"


def test_complete_clean_build_and_complete_error_build_are_distinguished():
    clean = combine_build_and_diagnostics(
        {"succeeded": True, "buildPerformed": True},
        {"complete": True, "messages": []},
    )
    assert clean["compiler_verified"] is True
    assert clean["success"] is True

    failed = combine_build_and_diagnostics(
        {"succeeded": False, "buildPerformed": True},
        {"diagnostics_complete": True, "messages": [
            {"Severity": "error", "ErrorCode": "C0006", "Text": "semicolon expected"},
            {"Severity": "warning", "Text": "unused"},
        ]},
    )
    assert failed["status"] == "failed"
    assert failed["errors"] == 1
    assert failed["warnings"] == 1
    assert failed["success"] is False

