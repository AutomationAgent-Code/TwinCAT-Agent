from pathlib import Path

import pytest

from tc_template.hmi_browser_target import _registered_view


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "Demo.hmiproj"
    project.write_text('<Project><ItemGroup><Content Include="Desktop.view" />'
                       '<Content Include="Views\\Main.view" /></ItemGroup></Project>', encoding="utf-8")
    (tmp_path / "Desktop.view").write_text('<div />', encoding="utf-8")
    (tmp_path / "Views").mkdir()
    (tmp_path / "Views/Main.view").write_text('<div />', encoding="utf-8")
    return project


def test_explicit_nested_view_must_exist_and_be_registered(tmp_path):
    project = _project(tmp_path)
    target, relative = _registered_view(str(project), "Views/Main.view")
    assert target == (tmp_path / "Views/Main.view").resolve()
    assert relative == "Views/Main.view"


@pytest.mark.parametrize("entry", ["../Other.view", "Missing.view", "Desktop.content"])
def test_invalid_explicit_view_is_rejected_before_browser(tmp_path, entry):
    with pytest.raises(ValueError):
        _registered_view(str(_project(tmp_path)), entry)
