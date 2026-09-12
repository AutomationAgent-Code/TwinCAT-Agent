from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tc_template import hmi_creation as hmi, _native_bridge, _ps_bridge


class FixtureSolution:
    def __init__(self, root, mode="success"):
        self.root, self.mode = root, mode
        self.FullName = str(root / "Machine.sln")
        Path(self.FullName).write_text("")
        self.file = root / "HMI1" / "HMI1.hmiproj"
        self.Projects = []
        self.creates = self.saves = 0

    def generate(self):
        self.file.parent.mkdir(exist_ok=True)
        self.file.write_text("<Project />")
        self.Projects = [SimpleNamespace(FullName=str(self.file), Kind="HMI")]

    def AddFromTemplate(self, template, destination, name, exclusive):
        self.creates += 1
        if self.mode == "create_error":
            raise RuntimeError("wizard failure")
        self.generate()
        if self.mode == "create_busy":
            raise RuntimeError(-2147418111, "busy after output")
        if self.mode == "solution_changed":
            self.FullName = str(self.root / "Other.sln")

    def SaveAs(self, path):
        self.saves += 1
        if self.mode == "save_timeout" or (self.mode == "save_busy" and self.saves < 3):
            raise RuntimeError(-2147418111, "busy saving")
        if self.mode == "save_error":
            raise PermissionError("access denied")
        if self.mode == "save_no_effect":
            return
        Path(path).write_text('Project("{type}") = "HMI1", "HMI1/HMI1.hmiproj", "{id}"\n')


def invoke(solution, **kwargs):
    template = solution.root / "Fixture.vstemplate"
    template.write_text("<VSTemplate />")
    with patch.object(hmi.time, "sleep"):
        return hmi.create_project(SimpleNamespace(Solution=solution),
                                  {"name": "HMI1", "template": str(template), "apply": True, **kwargs},
                                  wait_seconds=0, save_attempts=3)


@pytest.mark.parametrize("mode,saves", [("success", 1), ("create_busy", 1), ("save_busy", 3)])
def test_native_creation_reconciles_side_effects_and_busy_saves(tmp_path, mode, saves):
    solution = FixtureSolution(tmp_path, mode)
    result = invoke(solution)
    assert result["status"] == "created" and result["ok"]
    assert result["persisted_in_solution"] and result["verified_in_xae"]
    assert solution.creates == 1 and solution.saves == saves


def test_native_existing_loaded_project_is_recovered_without_wizard(tmp_path):
    solution = FixtureSolution(tmp_path)
    solution.generate()
    result = invoke(solution)
    assert result["status"] == "recovered"
    assert solution.creates == 0 and solution.saves == 1


def test_native_preview_current_unsaved_project_is_readonly(tmp_path):
    solution = FixtureSolution(tmp_path)
    solution.generate()
    before = Path(solution.FullName).read_bytes()
    result = invoke(solution, apply=False)
    assert result["status"] == "preview" and result["reuse_existing"]
    assert result["state"]["loaded_in_xae"]
    assert not result["state"]["persisted_in_solution"]
    assert solution.creates == solution.saves == 0
    assert Path(solution.FullName).read_bytes() == before


@pytest.mark.parametrize("mode", ["orphan", "other_path", "invalid_xml"])
def test_native_conflicting_or_invalid_existing_project_is_not_modified(tmp_path, mode):
    solution = FixtureSolution(tmp_path)
    solution.generate()
    if mode == "orphan":
        solution.Projects = []
    elif mode == "other_path":
        solution.Projects[0].FullName = str(tmp_path / "Other" / "HMI1.hmiproj")
    else:
        solution.file.write_text("invalid xml")
    before = solution.file.read_bytes()
    result = invoke(solution)
    assert not result["ok"] and result["phase"] == "inspect_existing"
    assert solution.creates == solution.saves == 0
    assert solution.file.read_bytes() == before


@pytest.mark.parametrize("mode,phase", [("create_error", "wait_for_project"),
    ("save_error", "save_solution"), ("save_timeout", "save_solution"),
    ("save_no_effect", "save_solution"), ("solution_changed", "wait_for_project")])
def test_native_failure_keeps_stage_and_never_claims_success(tmp_path, mode, phase):
    solution = FixtureSolution(tmp_path, mode)
    result = invoke(solution)
    assert not result["ok"] and result["phase"] == phase
    assert solution.creates == 1
    if mode in {"create_error", "solution_changed"}:
        assert solution.saves == 0
    if mode == "save_error":
        assert solution.saves == 1


def test_native_retry_only_saves_existing_output(tmp_path):
    solution = FixtureSolution(tmp_path, "save_timeout")
    assert invoke(solution)["retry_safe"]
    solution.mode = "success"
    assert invoke(solution)["status"] == "recovered"
    assert solution.creates == 1 and solution.saves == 4


def test_native_dispatch_never_retries_whole_create_command():
    with patch.object(_native_bridge, "create_hmi_project", side_effect=RuntimeError(-2147418111)) as create:
        with pytest.raises(RuntimeError):
            _native_bridge._dispatch_connected("hmi-create-project", {}, object())
    create.assert_called_once()


def test_hmi_creation_uses_native_dynamic_com_when_available():
    with patch.dict("os.environ", {"TC_AGENT_COM_BACKEND": "auto"}), \
         patch.object(_ps_bridge, "_native_helper", return_value=None), \
         patch.object(_native_bridge, "available", return_value=(True, "")), \
         patch.object(_ps_bridge, "_native_request", return_value={"status": "preview"}) as native, \
         patch.object(_ps_bridge.subprocess, "run") as powershell:
        assert _ps_bridge.ps_com("hmi-create-project", name="HMI1", apply=False)["status"] == "preview"
    native.assert_called_once()
    powershell.assert_not_called()
