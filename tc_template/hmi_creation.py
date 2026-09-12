"""Idempotent HMI creation/recovery using native dynamic DTE (no editor clicks)."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from ._com import is_com_busy_error


DEFAULT_TEMPLATE = Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates\ProjectTemplates\TcXaeShell\StarterPrj_PackagesConfig\StarterPrj.vstemplate")
SOLUTION_FOLDER = "{66a26720-8fb5-11d2-aa7e-00c04f688dde}"


def _path(value):
    return os.path.normcase(str(Path(str(value).replace("\\", "/")).resolve()))


def _projects(collection):
    for project in collection:
        yield project
        try:
            folder = str(project.Kind).lower() == SOLUTION_FOLDER
        except Exception:
            folder = False
        if folder:
            for item in project.ProjectItems:
                child = item.SubProject
                if child is not None:
                    yield from _projects([child])


def creation_state(dte, solution_file: Path, project_file: Path):
    state = dict(same_solution=None, file_exists=project_file.is_file(), file_valid=False,
                 loaded_in_xae=False, persisted_in_solution=False)
    if state["file_exists"]:
        try:
            state["file_valid"] = ET.parse(project_file).getroot().tag.rsplit("}", 1)[-1] == "Project"
        except (ET.ParseError, OSError):
            pass
    try:
        state["same_solution"] = _path(dte.Solution.FullName) == _path(solution_file)
        if not state["same_solution"]:
            return state
        for project in _projects(dte.Solution.Projects):
            try:
                full = str(project.FullName or "")
            except Exception as exc:
                if is_com_busy_error(exc):
                    raise
                continue
            if full and _path(full) == _path(project_file):
                state["loaded_in_xae"] = True
                break
        text = solution_file.read_text(encoding="utf-8-sig")
        for reference in re.findall(r'^Project\("[^"]+"\)\s*=\s*"[^"]+",\s*"([^"]+)",', text, re.M):
            if _path(solution_file.parent / reference.replace("\\", "/")) == _path(project_file):
                state["persisted_in_solution"] = True
                break
    except Exception as exc:
        if not is_com_busy_error(exc):
            raise
        state["busy"] = True
    return state


def create_project(dte, args: dict, *, wait_seconds=45, save_attempts=20):
    name = str(args.get("name") or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Invalid HMI project name")
    solution_name = str(dte.Solution.FullName or "")
    solution_file = Path(solution_name).resolve()
    if not solution_name or not solution_file.is_file():
        raise ValueError("Save/open a solution before creating an HMI project")
    destination = Path(args.get("output_directory") or solution_file.parent / name).resolve()
    project_file = destination / (name + ".hmiproj")
    template = Path(args.get("template") or DEFAULT_TEMPLATE).resolve()
    state = creation_state(dte, solution_file, project_file)
    if state["same_solution"] is not True:
        raise RuntimeError("XAE solution is changed or busy; no creation was attempted")
    existing = destination.exists() or state["loaded_in_xae"] or state["persisted_in_solution"]
    diagnostics = []
    base = dict(name=name, template=str(template), destination=str(destination), project_file=str(project_file))

    def incomplete(phase, error, retry_safe=False):
        return dict(base, status="incomplete", ok=False, phase=phase, error=error,
                    state=state, diagnostics=diagnostics, files_preserved=True, retry_safe=retry_safe,
                    next_action=("Resume this exact request after XAE is ready; it will save, not recreate."
                                 if retry_safe else "Inspect XAE loading and existing output. Do not delete files or blindly recreate."))

    if existing and not (state["file_valid"] and state["loaded_in_xae"]):
        return incomplete("inspect_existing", "Existing destination does not match a valid loaded HMI project")
    if not existing and (not template.is_file() or template.suffix.lower() != ".vstemplate"):
        raise FileNotFoundError(f"TwinCAT HMI template not found: {template}")
    if not args.get("apply", False):
        return dict(base, status="preview", apply_required=True, reuse_existing=existing, state=state)
    failed = busy = False
    if not existing:
        try:
            # Never retry this non-idempotent method after an ambiguous result.
            dte.Solution.AddFromTemplate(str(template), str(destination), name, False)
        except Exception as exc:
            failed, busy = True, is_com_busy_error(exc)
            diagnostics.append(dict(phase="add_from_template", message=str(exc), busy=busy))
    deadline = time.monotonic() + wait_seconds
    while True:
        state = creation_state(dte, solution_file, project_file)
        if state["same_solution"] is False:
            return incomplete("wait_for_project", "XAE solution changed; generated output was preserved")
        if state["file_valid"] and (state["loaded_in_xae"] or not existing):
            break
        if (failed and not busy) or time.monotonic() >= deadline:
            return incomplete("wait_for_project", "HMI wizard has not produced a valid loaded project")
        time.sleep(.25)
    for attempt in range(max(1, save_attempts)):
        if state["persisted_in_solution"]:
            break
        try:
            if _path(dte.Solution.FullName) != _path(solution_file):
                return incomplete("save_solution", "XAE solution changed; nothing was saved")
            # Persist registration only; leave unrelated edited documents alone.
            dte.Solution.SaveAs(str(solution_file))
        except Exception as exc:
            busy = is_com_busy_error(exc)
            diagnostics.append(dict(phase="save_solution", attempt=attempt + 1, message=str(exc), busy=busy))
            if not busy:
                break
        state = creation_state(dte, solution_file, project_file)
        if state["persisted_in_solution"]:
            break
        if attempt + 1 < save_attempts:
            time.sleep(.5)
    state = creation_state(dte, solution_file, project_file)
    if not all(state[key] for key in ("same_solution", "file_valid", "loaded_in_xae", "persisted_in_solution")):
        return incomplete("save_solution", "HMI output exists, but loaded identity and solution registration are not both verified",
                          state["same_solution"] is True and state["file_valid"] and state["loaded_in_xae"])
    return dict(base, status="recovered" if existing else "created", ok=True,
                verified_in_xae=True, persisted_in_solution=True, reused_existing=existing, diagnostics=diagnostics)
