"""
Project scaffolding engine.

Copies template files -> output directory, replacing all {{VAR}} and
{{GUID_N}} placeholders. _Libraries is copied raw.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .guid_utils import load_guid_map, generate_guid_map
from .models import TemplateMetadata, TemplateNotFoundError
from .repository import get_template, get_template_source_dir

RAW_DIRS = {"_Libraries"}
SKIP_DIRS = {".git", ".vs", "_CompileInfo", "_Boot"}
SKIP_FILES = {"template.yaml", "_GUID_map.json", ".suo"}
SKIP_SUFFIXES = {".bak"}


def _contained_destination(root: Path, name: str) -> Path:
    """Return a destination below root, rejecting traversal via substitutions."""
    candidate_name = Path(name)
    if (not name or candidate_name.is_absolute() or ".." in candidate_name.parts
            or len(candidate_name.parts) != 1):
        raise ValueError(f"Unsafe generated file name: {name!r}")
    root_resolved = root.resolve()
    candidate = (root_resolved / candidate_name).resolve()
    if candidate.parent != root_resolved:
        raise ValueError(f"Generated path escapes output directory: {name!r}")
    return candidate


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scaffold(
    template_names: list[str],
    output_dir: str | Path,
    *,
    user_vars: dict[str, str] | None = None,
    interactive: bool = True,
    dry_run: bool = False,
    no_hooks: bool = False,
    repo_dir: str | Path | None = None,
) -> dict:
    """Create a new TwinCAT project from a template.

    Args:
        template_names: Template name(s) to use. First is base, rest are overlays.
        output_dir: Where to create the project.
        user_vars: ``{PROJECT_NAME: ..., PLC_NAME: ...}``. If missing, prompts.
        interactive: If True, ask for missing variables.
        dry_run: Simulate only.
        no_hooks: Skip hook execution.
        repo_dir: Template repository path.

    Returns:
        Dict with output_dir, files_created, guid_count, timestamp.
    """
    output_dir = Path(output_dir)
    if user_vars is None:
        user_vars = {}

    if not template_names:
        raise ValueError("At least one template name is required")

    base_name = template_names[0]
    meta = get_template(base_name, repo_dir=repo_dir)
    src_dir = get_template_source_dir(base_name, repo_dir=repo_dir)

    # ---- Collect variables ----
    vars_ = _collect_vars(meta, user_vars, interactive)

    # ---- Build replacement map ----
    replacements: dict[str, str] = {}

    # Variable placeholders
    for var in meta.variables:
        replacements[f"{{{{{var.name}}}}}"] = vars_.get(var.name, var.default)

    # GUID placeholders: {{GUID_1}} -> fresh UUID
    guid_count = 0
    guid_map = load_guid_map(src_dir)
    if guid_map:
        fresh = generate_guid_map(len(guid_map))
        replacements.update(fresh)
        guid_count = len(fresh)

    # ---- Copy files ----
    counter = {"files": 0, "skipped": 0, "raw_copied": 0}
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    _copy_tree(src_dir, output_dir, replacements, dry_run, counter)

    # ---- Log ----
    result = {
        "output_dir": str(output_dir),
        "template_names": template_names,
        "template_version": meta.version,
        "guid_count": guid_count,
        "files_created": counter["files"],
        "files_skipped": counter["skipped"],
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not dry_run:
        log_path = output_dir / "_scaffold_log.json"
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False, default=str)

    return result


# ---------------------------------------------------------------------------
# Variable collection
# ---------------------------------------------------------------------------

def _collect_vars(
    meta: TemplateMetadata,
    user_vars: dict[str, str],
    interactive: bool,
) -> dict[str, str]:
    """Merge template defaults with user-provided values."""
    result: dict[str, str] = {}
    for var in meta.variables:
        result[var.name] = var.default
    result.update(user_vars)

    if interactive:
        import sys
        if sys.stdin.isatty():
            for var in meta.variables:
                if var.name in user_vars:
                    continue
                current = result[var.name]
                prompt = f">> {var.prompt} ({var.name})"
                if current:
                    prompt += f" [{current}]"
                prompt += ": "
                try:
                    ans = input(prompt).strip()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if ans:
                    result[var.name] = ans

    return result


# ---------------------------------------------------------------------------
# Recursive copy
# ---------------------------------------------------------------------------

def _copy_tree(
    src: Path,
    dst: Path,
    replacements: dict[str, str],
    dry_run: bool,
    counter: dict[str, int],
) -> None:
    """Copy src -> dst with placeholder replacement."""
    for item in sorted(src.iterdir()):
        if item.is_dir() and item.name in SKIP_DIRS:
            continue
        if item.is_file():
            if item.name in SKIP_FILES or item.suffix in SKIP_SUFFIXES:
                continue

        # Resolve name
        dest_name = item.name
        for old, new in replacements.items():
            dest_name = dest_name.replace(old, new)
        dest_path = _contained_destination(dst, dest_name)

        # Directory
        if item.is_dir():
            if item.name in RAW_DIRS:
                # _Libraries: raw copy, no processing
                if not dry_run:
                    shutil.copytree(item, dest_path)
                # Count raw files
                raw_count = sum(1 for _ in item.rglob("*") if _.is_file())
                counter["raw_copied"] += raw_count
                continue

            if not dry_run:
                dest_path.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, dest_path, replacements, dry_run, counter)
            continue

        # File
        counter["files"] += 1
        if dry_run:
            continue

        if _is_text(item.name):
            try:
                content = item.read_text(encoding="utf-8", errors="replace")
            except Exception:
                shutil.copy2(item, dest_path)
                continue
            for old, new in replacements.items():
                content = content.replace(old, new)
            dest_path.write_text(content, encoding="utf-8")
        else:
            shutil.copy2(item, dest_path)


def _is_text(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix in {
        ".sln", ".tsproj", ".plcproj",
        ".tcpou", ".tcdut", ".tcio", ".tctto",
        ".tcgtlo", ".tcvmo", ".tcvis",
        ".tmc", ".xti", ".xml", ".txt", ".json", ".csv",
    }
