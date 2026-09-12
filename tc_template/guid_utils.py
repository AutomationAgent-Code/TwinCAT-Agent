"""
GUID generation, scanning, classification, and replacement utilities.
"""

from __future__ import annotations

import json
import uuid
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import GUIDStrategy

# Standard GUID format: {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
GUID_RE = re.compile(
    r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}"
)

BECKHOFF_TYPE_PREFIX = "{18071995-"

# Well-known fixed GUIDs that should NEVER be replaced
FIXED_GUIDS = {
    "{B1E792BE-AA5F-4E3C-8C82-674BF9C0715B}",   # TcXaeShell project type
    "{08500001-0000-0000-F000-000000000064}",    # TcPlc30 CLSID
    "{00000000-0000-0000-0000-000000000000}",    # TwinCAT null GUID (sentinel)
}

# XML sections whose GUIDs are Beckhoff-defined and must NOT be replaced.
# Everything between these open/close tags is masked during GUID scanning.
PROTECTED_XML_SECTIONS = [
    ("<ProjectExtensions", "</ProjectExtensions>"),
    ("<PlaceholderReference", "</PlaceholderReference>"),
    ("<PlaceholderResolution", "</PlaceholderResolution>"),
    ("<LibraryReference", "</LibraryReference>"),
    ("<Licenses>", "</Licenses>"),
    ("<Device>", "</Device>"),
]

# Patterns for GUIDs that ARE safe to replace (project-specific contexts).
# We only scan these specific XML attributes — NOT the entire file content.
# This prevents accidentally replacing Beckhoff type-system GUIDs.
PROJECT_GUID_PATTERNS = [
    # Id="..." on ANY element (POU, Task, DUT, Interface, VisuElem, etc.)
    re.compile(r'\bId="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # Project GUID= in tsproj (element attribute)
    re.compile(r'<Project\s+GUID="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # ProjectGUID= in tsproj (case variations)
    re.compile(r'\bProjectGUID="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # ProjectGuid= in .plcproj (element content)
    re.compile(r'\bProjectGuid="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # <ProjectGuid>GUID</ProjectGuid> in .plcproj
    re.compile(r'<ProjectGuid>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</ProjectGuid>'),
    # <Application>GUID</Application> in .plcproj
    re.compile(r'<Application>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</Application>'),
    # <TypeSystem>GUID</TypeSystem>
    re.compile(r'<TypeSystem>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</TypeSystem>'),
    # <Implicit_Task_Info>GUID</Implicit_Task_Info>
    re.compile(r'<Implicit_Task_Info>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</Implicit_Task_Info>'),
    # <Implicit_KindOfTask>GUID</Implicit_KindOfTask>
    re.compile(r'<Implicit_KindOfTask>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</Implicit_KindOfTask>'),
    # <Implicit_Jitter_Distribution>GUID</Implicit_Jitter_Distribution>
    re.compile(r'<Implicit_Jitter_Distribution>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</Implicit_Jitter_Distribution>'),
    # <LibraryReferences>GUID</LibraryReferences>
    re.compile(r'<LibraryReferences>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</LibraryReferences>'),
    # SolutionGuid = {GUID} in .sln
    re.compile(r'SolutionGuid\s*=\s*(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})'),
    # @ProjectGuid in .tsproj (another format)
    re.compile(r'@ProjectGuid=(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})'),
    # GuidA / GuidB in tsproj axis I/O link definitions
    re.compile(r'\bGuidA="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    re.compile(r'\bGuidB="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # TmcHash in tsproj
    re.compile(r'\bTmcHash="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # FbGuid in TcVMO
    re.compile(r'<v n="FbGuid">(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</v>'),
    # Object GUIDs in .tcplcproj project extensions
    re.compile(r'<v>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</v>'),
    # GUIDs inside OptionKey names in ProjectExtensions (these ARE project-specific option refs)
    re.compile(r'Name="(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})"'),
    # CLSID in Instance elements
    re.compile(r'<CLSID[^>]*>(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})</CLSID>'),
]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate_guid() -> str:
    """Return a fresh GUID like ``{A1B2C3D4-...}``."""
    return "{" + str(uuid.uuid4()).upper() + "}"


def generate_guid_map(count: int) -> dict[str, str]:
    """Return ``{ {{GUID_1}}: new_guid, ... }`` for ``count`` placeholders."""
    return {f"{{{{GUID_{i}}}}}": generate_guid() for i in range(1, count + 1)}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def mask_protected_sections(content: str) -> str:
    """Remove GUIDs inside protected XML sections (Beckhoff-defined GUIDs).

    Replaces content between protected open/close tags with whitespace
    so that GUID scanning won't pick up GUIDs that must be preserved.
    """
    for open_tag, close_tag in PROTECTED_XML_SECTIONS:
        # Match from open_tag to its matching close tag
        # Use a simple non-greedy approach (handles nesting poorly but works
        # for TwinCAT XML files where these sections don't nest)
        pattern = re.compile(
            re.escape(open_tag) + r".*?" + re.escape(close_tag),
            re.DOTALL,
        )
        content = pattern.sub(
            lambda m: " " * len(m.group(0)),
            content,
        )
    return content


def scan_guids_in_file(filepath: Path, mask: bool = False) -> list[str]:
    """Return all GUID occurrences in a file (may contain duplicates).

    If ``mask`` is True, protected XML sections are stripped first and
    only project-specific GUID contexts are scanned.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        return []
    if mask:
        content = mask_protected_sections(content)
        return _scan_project_guids(content)
    return GUID_RE.findall(content)


def _scan_project_guids(content: str) -> list[str]:
    """Extract GUIDs only from project-specific XML contexts.

    This avoids capturing Beckhoff type system GUIDs (Name GUID=, Type GUID=,
    TcBaseType, Namespace="MC", etc.) that should never be replaced.
    """
    guids: list[str] = []
    for pattern in PROJECT_GUID_PATTERNS:
        guids.extend(pattern.findall(content))
    return guids


def scan_unique_guids(
    directory: Path,
    glob_pattern: str = "**/*",
    skip_dirs: set[str] | None = None,
    skip_files: set[str] | None = None,
    mask: bool = False,
) -> set[str]:
    """Walk a directory and return the set of unique GUIDs (uppercased).

    If ``mask`` is True, GUIDs in protected XML sections are excluded.
    """
    if skip_dirs is None:
        skip_dirs = {".git", ".vs", "_Libraries", "_CompileInfo"}
    if skip_files is None:
        skip_files = {".suo", ".bak"}

    guids: set[str] = set()
    for filepath in directory.rglob(glob_pattern):
        if not filepath.is_file():
            continue
        if any(d in filepath.parts for d in skip_dirs):
            continue
        if filepath.name in skip_files or filepath.suffix in skip_files:
            continue
        found = scan_guids_in_file(filepath, mask=mask)
        guids.update(g.upper() for g in found)
    return guids


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_system_guid(
    guid: str,
    strategy: "GUIDStrategy | None" = None,
) -> bool:
    """True if this GUID should NOT be replaced (Beckhoff system GUID)."""
    g = guid.upper()

    if g.startswith(BECKHOFF_TYPE_PREFIX):
        return True

    if g in FIXED_GUIDS:
        return True

    if strategy is not None:
        if any(g.startswith(p.upper()) for p in strategy.exclude_prefixes):
            return True
        if g in {e.upper() for e in strategy.exclude_exact}:
            return True

    return False


def classify_guids(
    guids: set[str],
    strategy: "GUIDStrategy | None" = None,
) -> tuple[set[str], set[str]]:
    """Split guids into (project_guids, system_guids)."""
    project: set[str] = set()
    system: set[str] = set()
    for g in guids:
        if is_system_guid(g, strategy):
            system.add(g)
        else:
            project.add(g)
    return project, system


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------

def replace_guids_in_content(content: str, guid_map: dict[str, str]) -> str:
    """Replace all GUIDs in ``content`` according to ``guid_map`` (case-insensitive)."""
    def _replace(m: re.Match) -> str:
        found = m.group(0)
        found_upper = found.upper()
        for old, new in guid_map.items():
            if old.upper() == found_upper:
                return new
        return found
    return GUID_RE.sub(_replace, content)


def replace_placeholders_in_content(content: str, replacements: dict[str, str]) -> str:
    """Replace ``{{VAR}}`` placeholders with values."""
    result = content
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


# ---------------------------------------------------------------------------
# GUID map file I/O (placeholder strategy)
# ---------------------------------------------------------------------------

def load_guid_map(template_dir: Path) -> dict[str, str] | None:
    """Load _GUID_map.json; return None if absent."""
    map_path = template_dir / "_GUID_map.json"
    if not map_path.exists():
        return None
    with open(map_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_guid_map(template_dir: Path, guid_map: dict[str, str]) -> None:
    """Write _GUID_map.json."""
    map_path = template_dir / "_GUID_map.json"
    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump(guid_map, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Scan-replace strategy
# ---------------------------------------------------------------------------

def build_scan_replace_map(
    template_dir: Path,
    strategy: "GUIDStrategy",
) -> dict[str, str]:
    """Scan all files, classify GUIDs, return {old_project_GUID: fresh_GUID}."""
    all_guids = scan_unique_guids(template_dir)
    project_guids, _ = classify_guids(all_guids, strategy)
    return {g: generate_guid() for g in sorted(project_guids)}
