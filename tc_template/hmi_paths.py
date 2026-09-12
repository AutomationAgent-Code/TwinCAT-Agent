"""Exact, fail-closed HMI project path resolution.

HMI projects commonly contain the same basename in different folders.  A
basename or suffix match is therefore never a valid project-item lookup.  The
helpers in this module keep the path contract in one place for planning and
offline validation; callers that need a file registered in ``.hmiproj`` must
use :func:`resolve_registered`.
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET


# TE2000's standard folders are deliberately named things such as
# ``00 Content`` and ``100 UserControl``.  Item *names* remain stricter in the
# creation tools; project paths must accept these official folder names.
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. -]*\Z")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_PROTECTED_ROOTS = {"properties", "server", "bin", "obj", "packages", ".twincatagent"}


class HmiPathError(ValueError):
    """A path is invalid or cannot be resolved unambiguously."""


def normalize_relative(relative: str, *, reject_reserved: bool = True) -> str:
    """Return a canonical project-relative path or fail closed.

    This deliberately rejects rooted paths, traversal, empty segments and
    Windows device/alternate-stream syntax instead of attempting to sanitize
    them.  The returned separator is always ``/`` and comparison is performed
    case-insensitively by :func:`key`.
    """
    value = str(relative or "").replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise HmiPathError("HMI file path must be relative to the project.")
    if any(ord(char) < 32 for char in value) or any(char in value for char in '<>"|?*:'):
        raise HmiPathError("HMI file path contains illegal characters.")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise HmiPathError("HMI file path cannot contain empty or traversal segments.")
    for part in parts:
        if (not _SEGMENT.fullmatch(part) or part != part.strip() or
                part.endswith(".") or part.upper().split(".", 1)[0] in _RESERVED):
            raise HmiPathError("Use safe identifier segments for HMI project paths.")
    if reject_reserved and parts[0].casefold() in _PROTECTED_ROOTS:
        raise HmiPathError("Reserved HMI configuration/output directory.")
    return "/".join(parts)


def key(relative: str) -> str:
    """Canonical comparison key for an Include or project-relative path."""
    value = str(relative or "").replace("\\", "/").strip()
    while value.endswith("/"):
        value = value[:-1]
    return value.casefold()


def resolve_under(root: Path, relative: str, *, reject_reserved: bool = True) -> tuple[str, Path]:
    """Resolve a safe path under *root* without requiring it to exist."""
    canonical = normalize_relative(relative, reject_reserved=reject_reserved)
    project_root = Path(root).resolve()
    target = (project_root / Path(canonical)).resolve()
    if not target.is_relative_to(project_root) or target == project_root:
        raise HmiPathError("HMI file path escapes the project directory.")
    return canonical, target


def project_includes(project_file: str | Path) -> list[tuple[str, ET.Element]]:
    """Return every ``Include`` node in document order."""
    document = ET.parse(project_file)
    return [
        (str(node.get("Include")), node)
        for node in document.getroot().iter()
        if node.get("Include")
    ]


def registered_matches(project_file: str | Path, relative: str) -> list[tuple[str, ET.Element]]:
    """Find exact Include matches; never matches by basename or suffix."""
    canonical = normalize_relative(relative)
    expected = key(canonical)
    return [(include, node) for include, node in project_includes(project_file)
            if key(include) == expected]


def resolve_registered(project_file: str | Path, relative: str,
                       *, require_file: bool = True) -> tuple[str, Path, ET.Element]:
    """Resolve one and only one registered project item.

    A missing or duplicate exact registration is an error.  In particular,
    ``Desktop.view`` cannot resolve ``00 Content/Desktop.view``.
    """
    project = Path(project_file).resolve()
    canonical, target = resolve_under(project.parent, relative, reject_reserved=False)
    matches = registered_matches(project, canonical)
    if len(matches) == 0:
        raise HmiPathError(f"HMI item is not registered at exact path: {canonical}")
    if len(matches) > 1:
        raise HmiPathError(f"HMI item has duplicate exact registrations: {canonical}")
    if require_file and not target.exists():
        raise HmiPathError(f"HMI item file is missing at exact path: {canonical}")
    return canonical, target, matches[0][1]
