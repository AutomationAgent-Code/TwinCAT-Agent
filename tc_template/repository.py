"""
Template repository CRUD operations.

Each template is a subdirectory under the repository root containing
``template.json`` (metadata) and a ``template/`` folder (source files).

Default repository root: ``<project_root>/Repository/``
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    TemplateMetadata,
    TemplateNotFoundError,
    TemplateValidationError,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_SAFE_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_child(root: Path, child: str, *, label: str) -> Path:
    """Resolve a single repository child and prove it stays below *root*."""
    if not isinstance(child, str) or not _SAFE_TEMPLATE_NAME.fullmatch(child):
        raise TemplateValidationError(
            f"Invalid {label}: expected a single name using letters, digits, '.', '_' or '-'"
        )
    root_resolved = root.resolve()
    candidate = (root_resolved / child).resolve()
    if candidate.parent != root_resolved:
        raise TemplateValidationError(f"Invalid {label}: path escapes repository root")
    return candidate


def _safe_relative_dir(root: Path, relative: str, *, label: str) -> Path:
    rel = Path(relative)
    if not relative or rel.is_absolute() or ".." in rel.parts:
        raise TemplateValidationError(f"Invalid {label}: must be a relative contained path")
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise TemplateValidationError(f"Invalid {label}: path escapes template root")
    return candidate

def _resolve_repo_dir(repo_dir: str | Path | None = None) -> Path:
    """Resolve template repository root. Defaults to ``Repository/`` sibling."""
    if repo_dir is not None:
        return Path(repo_dir)
    package_dir = Path(__file__).resolve().parent   # tc_template/
    project_root = package_dir.parent
    return project_root / "Repository"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_templates(
    *,
    category: str | None = None,
    tag: str | None = None,
    repo_dir: str | Path | None = None,
) -> list[TemplateMetadata]:
    """List templates, optionally filtered by category / tag."""
    repo = _resolve_repo_dir(repo_dir)
    if not repo.exists():
        return []

    result: list[TemplateMetadata] = []
    for entry in sorted(repo.iterdir()):
        if not entry.is_dir():
            continue
        yaml_file = entry / "template.yaml"
        if not yaml_file.exists():
            continue
        try:
            meta = TemplateMetadata.from_yaml(yaml_file)
        except Exception:
            continue
        if category and meta.category != category:
            continue
        if tag and tag not in meta.tags:
            continue
        result.append(meta)
    return result


def get_template(
    name: str,
    *,
    repo_dir: str | Path | None = None,
) -> TemplateMetadata:
    """Load a single template's metadata. Raises TemplateNotFoundError."""
    repo = _resolve_repo_dir(repo_dir)
    yaml_file = _safe_child(repo, name, label="template name") / "template.yaml"
    if not yaml_file.exists():
        available = [d.name for d in repo.iterdir()
                     if d.is_dir() and (d / "template.yaml").exists()]
        raise TemplateNotFoundError(
            f"Template '{name}' not found. "
            f"Available: {', '.join(sorted(available)) if available else '(none)'}"
        )
    return TemplateMetadata.from_yaml(yaml_file)


def get_template_dir(
    name: str,
    *,
    repo_dir: str | Path | None = None,
) -> Path:
    """Return the template's root directory path."""
    repo = _resolve_repo_dir(repo_dir)
    tmpl_dir = _safe_child(repo, name, label="template name")
    if not tmpl_dir.is_dir() or not (tmpl_dir / "template.yaml").exists():
        raise TemplateNotFoundError(f"Template '{name}' not found at {tmpl_dir}")
    return tmpl_dir


def get_template_source_dir(
    name: str,
    *,
    repo_dir: str | Path | None = None,
) -> Path:
    """Return the template's ``template/`` source directory."""
    meta = get_template(name, repo_dir=repo_dir)
    tmpl_root = get_template_dir(name, repo_dir=repo_dir)
    src_dir = _safe_relative_dir(tmpl_root, meta.template_dir, label="template_dir")
    if not src_dir.exists():
        raise TemplateNotFoundError(
            f"Template source dir not found: {src_dir}"
        )
    return src_dir


def add_template(
    source_dir: str | Path,
    metadata: TemplateMetadata,
    *,
    repo_dir: str | Path | None = None,
) -> Path:
    """Copy source files into the repo and register a new template.

    Args:
        source_dir: Path to already-processed template source files
                    (with placeholders in place).
        metadata: TemplateMetadata to write as template.yaml.
        repo_dir: Repository root.

    Returns:
        Path to the new template directory.
    """
    repo = _resolve_repo_dir(repo_dir)
    dest_dir = _safe_child(repo, metadata.name, label="template name")
    template_dest = _safe_relative_dir(dest_dir, metadata.template_dir, label="template_dir")

    if dest_dir.exists():
        raise FileExistsError(f"Template '{metadata.name}' already exists at {dest_dir}")

    repo.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True)

    # Copy source files into template/ subdirectory
    shutil.copytree(Path(source_dir), template_dest)

    # Write template.yaml
    yaml_path = dest_dir / "template.yaml"
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write(metadata.to_yaml())

    return dest_dir


def remove_template(
    name: str,
    *,
    repo_dir: str | Path | None = None,
) -> None:
    """Delete a template from the repository."""
    repo = _resolve_repo_dir(repo_dir)
    tmpl_dir = _safe_child(repo, name, label="template name")
    if not tmpl_dir.is_dir():
        raise TemplateNotFoundError(f"Template '{name}' not found")
    shutil.rmtree(tmpl_dir)


def validate_template(
    name: str,
    *,
    repo_dir: str | Path | None = None,
) -> tuple[bool, list[str]]:
    """Validate template; return (is_valid, issues)."""
    try:
        meta = get_template(name, repo_dir=repo_dir)
    except TemplateNotFoundError as e:
        return False, [str(e)]

    issues = meta.validate()

    # Check source directory exists
    try:
        get_template_source_dir(name, repo_dir=repo_dir)
    except TemplateNotFoundError as e:
        issues.append(str(e))

    return len(issues) == 0, issues


def auto_detect_template_info(
    project_dir: str | Path,
) -> dict:
    """Quick-scan a TwinCAT project and return suggested template metadata."""
    project_dir = Path(project_dir)

    info: dict = {
        "suggested_name": project_dir.name.lower().replace(" ", "-"),
        "file_count": 0,
        "guid_count": 0,
        "has_libraries": False,
        "library_count": 0,
        "has_visualization": False,
        "is_twincat_project": False,
    }

    sln_files = list(project_dir.glob("*.sln"))
    tsproj_files = list(project_dir.glob("*.tsproj"))
    if sln_files and tsproj_files:
        info["is_twincat_project"] = True

    info["file_count"] = sum(1 for _ in project_dir.rglob("*") if _.is_file())

    lib_dirs = list(project_dir.rglob("_Libraries"))
    if lib_dirs:
        info["has_libraries"] = True
        info["library_count"] = sum(1 for _ in lib_dirs[0].rglob("*.library"))

    vis_files = list(project_dir.rglob("*.TcVIS"))
    if vis_files:
        info["has_visualization"] = True

    return info
