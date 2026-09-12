import json
import zipfile
from pathlib import Path

from tc_agent.reporting import create_report, sanitize


def test_sanitize_removes_credentials_and_source_code():
    value = sanitize({
        "api_key": "sk-abcdefghijklmnop",
        "authorization": "Bearer abc.def",
        "code": "PROGRAM MAIN\nnValue := 1;",
        "message": "token=super-secret failure",
    })
    assert value["api_key"] == "<redacted>"
    assert value["authorization"] == "<redacted>"
    assert value["code"].startswith("<omitted")
    assert "super-secret" not in value["message"]


def test_bug_report_bundle_contains_markdown_json_and_sanitized_diagnostics(tmp_path: Path):
    solution = tmp_path / "Demo.sln"
    conversation = {
        "database": str(tmp_path / ".TwinCATAgent" / "agent.db"),
        "thread": {"id": "thread-1", "title": "构建失败", "status": "idle"},
        "events": [
            {"type": "tool_use", "name": "plc_write",
             "input": {"name": "MAIN", "code": "PROGRAM MAIN\nEND_PROGRAM"}},
            {"type": "tool_result", "name": "plc_build",
             "result": {"error": "token=private-value C0004 member missing"}},
        ],
        "execution_trace": [{
            "run": {"id": "run-1", "request_text": "PROGRAM SECRET", "status": "failed"},
            "steps": [{"id": "step-1", "status": "completed",
                       "output": {"text": "PROGRAM SECRET"}}],
            "tool_executions": [{
                "id": "exec-1", "step_id": "step-1", "tool_call_id": "call-1",
                "tool_name": "plc_write", "status": "failed",
                "args": {"code": "PROGRAM SECRET"},
                "result": {"authorization": "Bearer hidden"},
            }],
            "approvals": [{"id": "approval-1", "status": "approved",
                           "request": {"code": "PROGRAM SECRET"}}],
        }],
    }
    result = create_report(
        project_dir=tmp_path,
        agent_version="1.2.3",
        solution=str(solution),
        conversation=conversation,
        xae={"pid": 123, "solution": str(solution), "api_key": "sk-hidden-value"},
        args={
            "report_type": "bug", "title": "编译错误读取失败",
            "description": "点击编译后错误数仍为 0",
            "steps": ["打开工程", "调用 plc_build"],
            "expected": "返回真实错误", "actual": "errorCount 为 0",
        },
    )
    archive = Path(result["file"])
    assert archive.parent == tmp_path / ".TwinCATAgent" / "reports"
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"report.md", "report.json"}
        markdown = bundle.read("report.md").decode("utf-8")
        payload_text = bundle.read("report.json").decode("utf-8")
        payload = json.loads(payload_text)
    assert "编译错误读取失败" in markdown
    assert payload["xae"]["api_key"] == "<redacted>"
    assert payload["recent_tool_events"][0]["args"]["code"].startswith("<omitted")
    assert "private-value" not in payload_text
    assert "PROGRAM MAIN" not in payload_text
    assert payload["execution_trace"][0]["run"]["id"] == "run-1"
    assert payload["execution_trace"][0]["tools"][0]["id"] == "exec-1"
    assert "PROGRAM SECRET" not in payload_text
    assert "Bearer hidden" not in payload_text


def test_suggestion_report_uses_proposal_sections(tmp_path: Path):
    result = create_report(
        project_dir=tmp_path, agent_version="1.0", solution="",
        conversation={"thread": {}, "events": []}, xae={},
        args={
            "report_type": "suggestion", "title": "增加模板预览",
            "description": "希望写入前看到生成结构",
            "proposal": "增加只读预览工具", "benefit": "减少返工",
        },
    )
    with zipfile.ZipFile(result["file"]) as bundle:
        markdown = bundle.read("report.md").decode("utf-8")
    assert "建议方案" in markdown
    assert "预期收益" in markdown
