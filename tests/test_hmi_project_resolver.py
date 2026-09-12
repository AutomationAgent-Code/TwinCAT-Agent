from pathlib import Path
from unittest.mock import patch

import pytest

from tc_template import _ps_bridge as bridge


def _solution(tmp_path: Path, projects: list[str]) -> Path:
    rows = [
        f'Project("{{TYPE-{index}}}") = "{Path(name).stem}", "{name}", "{{ID-{index}}}"\nEndProject'
        for index, name in enumerate(projects)
    ]
    path = tmp_path / "Mixed.sln"
    path.write_text("\n".join(rows), encoding="utf-8-sig")
    for relative in projects:
        file = tmp_path / Path(relative.replace("\\", "/"))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("<Project />", encoding="utf-8")
    return path


def test_nested_single_hmi_is_bound_to_exact_solution_path(tmp_path):
    solution = _solution(tmp_path, [r"HMI\Nested\Line.hmiproj"])
    calls = []

    def raw(command, timeout=120.0, **args):
        calls.append((command, args))
        if command == "connect-check":
            return {"solution": str(solution), "pid": 2008}
        return {"status": "ok", "project": args.get("project")}

    with patch.object(bridge, "_ps_com_raw", side_effect=raw):
        result = bridge.ps_com("hmi-project-info", project="")
    expected = str((tmp_path / "HMI/Nested/Line.hmiproj").resolve())
    assert result["project"] == expected
    assert calls == [("connect-check", {}), ("hmi-project-info", {"project": expected})]


def test_multiple_hmi_projects_require_explicit_selector(tmp_path):
    solution = _solution(tmp_path, [r"A\A.hmiproj", r"B\B.hmiproj"])
    with patch.object(bridge, "_ps_com_raw", return_value={"solution": str(solution), "pid": 2008,
            "active_document": {"full_name": str(tmp_path / "B/Main.view")}}) as raw:
        with pytest.raises(bridge.TcComError, match="specify project explicitly"):
            bridge.ps_com("hmi-project-info", project="")
    raw.assert_called_once_with("connect-check", 20.0)


def test_auto_resolver_refuses_pid_mismatch(tmp_path):
    solution = _solution(tmp_path, [r"HMI\Line.hmiproj"])
    with patch.object(bridge, "_ps_com_raw", return_value={"solution": str(solution), "pid": 99}):
        with bridge.tool_target(42), pytest.raises(bridge.TcComError, match="expected PID 42"):
            bridge.ps_com("hmi-project-info", project="")


def test_explicit_project_does_not_run_auto_resolver():
    with patch.object(bridge, "_ps_com_raw", return_value={"status": "ok"}) as raw:
        bridge.ps_com("hmi-project-info", project=r"G:\HMI\Line.hmiproj")
    raw.assert_called_once_with("hmi-project-info", 120.0, project=r"G:\HMI\Line.hmiproj")


def test_projectless_framework_template_catalog_stays_projectless():
    with patch.object(bridge, "_ps_com_raw", return_value={"status": "ok"}) as raw:
        bridge.ps_com("hmi-framework-templates")
    raw.assert_called_once_with("hmi-framework-templates", 120.0)
