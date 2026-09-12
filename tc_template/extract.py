"""
Template extraction — create a template from an existing TwinCAT project.

Scans a project directory, classifies GUIDs, replaces project-specific
GUIDs with placeholders, and generates template.yaml + _GUID_map.json.

**_Libraries directory is copied AS-IS — no processing whatsoever.**
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from .guid_utils import (
    GUID_RE,
    scan_unique_guids,
    classify_guids,
    save_guid_map,
    replace_guids_in_content,
    is_system_guid,
)
from .models import (
    TemplateMetadata,
    Variable,
    GUIDStrategy,
    FileRules,
)
from .repository import add_template, _resolve_repo_dir

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Dir/file patterns to skip entirely
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", ".vs", "_CompileInfo", "_Boot"}
SKIP_FILES = {".suo"}
SKIP_SUFFIXES = {".bak"}

# ---------------------------------------------------------------------------
# Dir that gets copied RAW (no placeholder/GUID processing)
# ---------------------------------------------------------------------------

RAW_DIRS = {"_Libraries"}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_template(
    project_dir: str | Path,
    template_name: str,
    *,
    project_name: str | None = None,
    plc_name: str | None = None,
    interactive: bool = True,
    auto_detect: bool = False,
    repo_dir: str | Path | None = None,
) -> TemplateMetadata:
    """Extract a TwinCAT project as a new template.

    Steps:
    1. Scan project structure
    2. Detect or accept PROJECT_NAME / PLC_NAME
    3. Scan all files for GUIDs, classify → system vs project
    4. Build GUID map: project-GUID → {{GUID_N}}
    5. Copy files to Repository/<name>/template/ with replacements
    6. Write _GUID_map.json
    7. Register via add_template (writes template.yaml)

    **_Libraries is copied raw — no placeholder or GUID replacement.**

    Args:
        project_dir: Path to the TwinCAT project.
        template_name: Name for the new template (kebab-case).
        project_name: Original project name (auto-detected from .sln if None).
        plc_name: Original PLC name (auto-detected if None).
        interactive: Reserved for future interactive variable detection.
        auto_detect: If True, skip prompts and use auto-detected values.
        repo_dir: Repository root (default: ``<project>/Repository/``).

    Returns:
        The created TemplateMetadata.
    """
    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    # ---- Phase 1: Detect project structure ----
    result = analyze_project(project_dir)
    if not result["is_twincat_project"]:
        raise ValueError(
            f"'{project_dir}' does not appear to be a TwinCAT project "
            f"(no .sln + .tsproj found)"
        )

    if project_name is None:
        project_name = result.get("detected_project_name", project_dir.name)
        # Clean spaces → keep them (they appear in .sln names)
    if plc_name is None:
        plc_name = result.get("detected_plc_name", "PLC1")

    print(f"\n>>> Extracting template: {template_name}")
    print(f"    Source: {project_dir}")
    print(f"    Project: {project_name}  ->  {{{{PROJECT_NAME}}}}")
    print(f"    PLC:     {plc_name}  ->  {{{{PLC_NAME}}}}")

    # ---- Phase 2: Scan GUIDs (excluding _Libraries, masking protected XML) ----
    print(f"\n>>> Scanning GUIDs (skipping _Libraries, masking system sections)...")
    all_guids = scan_unique_guids(project_dir, skip_dirs=SKIP_DIRS | RAW_DIRS, mask=True)
    project_guids, system_guids = classify_guids(all_guids)

    print(f"    Total unique GUIDs: {len(all_guids)}")
    print(f"    Project GUIDs: {len(project_guids)} (will be placeholderized)")
    print(f"    System GUIDs: {len(system_guids)} (preserved)")

    # Build GUID map: original → {{GUID_N}}
    # Sort for deterministic ordering
    sorted_project_guids = sorted(project_guids)
    guid_map: dict[str, str] = {}
    guid_map_reverse: dict[str, str] = {}  # {{GUID_N}} → original

    for i, guid in enumerate(sorted_project_guids, start=1):
        placeholder = f"{{{{GUID_{i}}}}}"
        guid_map[guid] = placeholder
        guid_map_reverse[placeholder] = guid

    # ---- Phase 3: Copy files with replacement ----
    repo = _resolve_repo_dir(repo_dir)
    template_dest_dir = repo / template_name / "template"
    if template_dest_dir.exists():
        raise FileExistsError(f"Template destination already exists: {template_dest_dir}")
    template_dest_dir.mkdir(parents=True)

    stats = {"copied": 0, "skipped": 0, "raw_copied": 0}

    _copy_with_replacements(
        src=project_dir,
        dst=template_dest_dir,
        project_name=project_name,
        plc_name=plc_name,
        guid_map=guid_map,
        stats=stats,
    )

    print(f"\n>>> File processing:")
    print(f"    Processed: {stats['copied']} files (placeholder/GUID replacement)")
    print(f"    Raw copy:  {stats['raw_copied']} files (_Libraries untouched)")
    print(f"    Skipped:   {stats['skipped']} files")

    # ---- Phase 4: Write _GUID_map.json ----
    save_guid_map(template_dest_dir, guid_map_reverse)
    print(f"\n>>> _GUID_map.json: {len(guid_map_reverse)} GUID mappings")

    # ---- Phase 5: Build and write template metadata ----
    description = _generate_description(project_dir, project_name, result)
    print(f"\n>>> Description: {description}")

    metadata = TemplateMetadata(
        name=template_name,
        version="0.1.0",
        display_name=template_name.replace("-", " ").title(),
        description=description,
        author="",
        license="MIT",
        tags=[],
        category="plc",
        template_dir="template",
        variables=[
            Variable(
                name="PROJECT_NAME",
                prompt="Project name (TwinCAT Solution)",
                default="MyProject",
                required=True,
                pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$",
                error_msg="Must start with letter, only letters/digits/underscore",
            ),
            Variable(
                name="PLC_NAME",
                prompt="PLC name",
                default=plc_name,
                required=True,
                pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$",
                error_msg="Must start with letter, only letters/digits/underscore",
            ),
        ],
        guid=GUIDStrategy(
            strategy="placeholder",
            exclude_prefixes=["{18071995-"],
            exclude_exact=[],
        ),
    )

    # Write template.yaml
    yaml_path = repo / template_name / "template.yaml"
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write(metadata.to_yaml())

    print(f"\n>>> Template created: {repo / template_name}")
    print(f"    template.yaml: {yaml_path}")
    print(f"    Source files: {template_dest_dir}")

    return metadata


# ---------------------------------------------------------------------------
# Recursive copy with full replacement
# ---------------------------------------------------------------------------

def _copy_with_replacements(
    src: Path,
    dst: Path,
    project_name: str,
    plc_name: str,
    guid_map: dict[str, str],
    stats: dict[str, int],
) -> None:
    """Recursively copy ``src`` → ``dst``, applying all replacements.

    Rules:
    - SKIP_DIRS: skip entirely
    - RAW_DIRS (_Libraries): copy raw, no processing
    - Text files (.sln, .xml, .TcPOU, etc.): replace project_name, plc_name, GUIDs
    - Binary files (.library, .xlsx): raw copy
    - Directory/file names: replace project_name, plc_name
    """
    # Build the set of name replacements
    name_replacements: dict[str, str] = {}

    # Only do name replacement if the names differ from default placeholders
    if project_name != "{{PROJECT_NAME}}":
        name_replacements[project_name] = "{{PROJECT_NAME}}"
    if plc_name != "{{PLC_NAME}}":
        name_replacements[plc_name] = "{{PLC_NAME}}"

    # Merge GUID map into replacements for content processing
    # NOTE: GUID replacement is case-insensitive via replace_guids_in_content
    # Name replacement is case-sensitive via str.replace

    _copy_recursive(
        src=src,
        dst=dst,
        name_replacements=name_replacements,
        guid_map=guid_map,
        stats=stats,
        src_root=src,
    )


def _copy_recursive(
    src: Path,
    dst: Path,
    name_replacements: dict[str, str],
    guid_map: dict[str, str],
    stats: dict[str, int],
    src_root: Path,
) -> None:
    """Internal recursive copy."""
    for item in sorted(src.iterdir()):
        # --- Directory skip ---
        if item.is_dir() and item.name in SKIP_DIRS:
            stats["skipped"] += 1
            continue

        # --- File skip ---
        if item.is_file():
            if item.name in SKIP_FILES or item.suffix in SKIP_SUFFIXES:
                stats["skipped"] += 1
                continue

        # --- Resolve destination name ---
        dest_name = item.name
        for old, new in name_replacements.items():
            dest_name = dest_name.replace(old, new)
        dest_path = dst / dest_name

        # --- Directory ---
        if item.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)

            # _Libraries -> raw copy, no processing
            if item.name in RAW_DIRS:
                for lib_item in item.rglob("*"):
                    if lib_item.is_dir():
                        continue
                    rel = lib_item.relative_to(item)
                    lib_dst = dest_path / rel
                    lib_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(lib_item, lib_dst)
                    stats["raw_copied"] += 1
                continue

            # Recurse into normal directories
            _copy_recursive(
                src=item,
                dst=dest_path,
                name_replacements=name_replacements,
                guid_map=guid_map,
                stats=stats,
                src_root=src_root,
            )
            continue

        # --- File ---
        if _is_text_file(item.name):
            try:
                with open(item, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception:
                shutil.copy2(item, dest_path)
                stats["copied"] += 1
                continue

            # Step 1: case-sensitive name replacements
            for old, new in name_replacements.items():
                content = content.replace(old, new)

            # Step 2: case-INSENSITIVE GUID replacement
            if guid_map:
                content = replace_guids_in_content(content, guid_map)

            with open(dest_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            stats["copied"] += 1
        else:
            # Binary or unknown -- raw copy
            shutil.copy2(item, dest_path)
            stats["copied"] += 1


def _is_text_file(filename: str) -> bool:
    """Check if a file should undergo text replacement."""
    ext = Path(filename).suffix.lower()
    return ext in {
        ".sln", ".tsproj", ".plcproj",
        ".tcpou", ".tcdut", ".tcio", ".tctto",
        ".tcgtlo", ".tcvmo", ".tcvis",
        ".tmc", ".xti", ".xml", ".txt", ".json", ".csv",
    }


# ---------------------------------------------------------------------------
# Project analysis
# ---------------------------------------------------------------------------

def analyze_project(
    project_dir: str | Path,
) -> dict:
    """Analyze a TwinCAT project without extracting.

    Returns a dict with:
      suggested_name, file_count, guid_count, system_guid_count,
      has_libraries, library_count, has_visualization,
      detected_project_name, detected_plc_name,
      is_twincat_project
    """
    project_dir = Path(project_dir)

    result: dict = {
        "suggested_name": project_dir.name.lower().replace(" ", "-"),
        "file_count": 0,
        "guid_count": 0,
        "system_guid_count": 0,
        "has_libraries": False,
        "library_count": 0,
        "has_visualization": False,
        "detected_project_name": project_dir.name,
        "detected_plc_name": "PLC1",
        "is_twincat_project": False,
    }

    # Check if this looks like a TwinCAT project
    sln_files = list(project_dir.glob("*.sln"))
    tsproj_files = list(project_dir.glob("*.tsproj")) + list(project_dir.glob("*/*.tsproj"))
    if sln_files and tsproj_files:
        result["is_twincat_project"] = True
        # Try to extract project name from .sln (line like: Project("...") = "Name", "...tsproj", "...")
        try:
            with open(sln_files[0], "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if 'Project(' in line and '.tsproj' in line:
                        # Extract name between = " and ",
                        import re
                        m = re.search(r'=\s*"([^"]+)"', line)
                        if m:
                            result["detected_project_name"] = m.group(1)
                        break
        except Exception:
            pass

    # Detect PLC name from plcproj
    plcproj_files = list(project_dir.rglob("*.plcproj"))
    if plcproj_files:
        try:
            with open(plcproj_files[0], "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            import re
            m = re.search(r"<Name>([^<]+)</Name>", content)
            if m:
                result["detected_plc_name"] = m.group(1)
        except Exception:
            pass

    # Count files
    total = 0
    for f in project_dir.rglob("*"):
        if f.is_file():
            # Skip dirs we'd skip during extraction
            if any(d in f.parts for d in SKIP_DIRS):
                continue
            if f.name in SKIP_FILES or f.suffix in SKIP_SUFFIXES:
                continue
            total += 1
    result["file_count"] = total

    # Check for libraries
    lib_dirs = list(project_dir.rglob("_Libraries"))
    if lib_dirs:
        result["has_libraries"] = True
        result["library_count"] = sum(1 for _ in lib_dirs[0].rglob("*.library"))

    # Count PLC object types
    result["pou_count"] = sum(1 for _ in project_dir.rglob("*.TcPOU"))
    result["dut_count"] = sum(1 for _ in project_dir.rglob("*.TcDUT"))
    result["gvl_count"] = sum(1 for _ in project_dir.rglob("*.TcGVL"))
    result["vis_count"] = sum(1 for _ in project_dir.rglob("*.TcVIS"))
    result["has_visualization"] = result["vis_count"] > 0

    # GUID stats
    try:
        all_guids = scan_unique_guids(project_dir, skip_dirs=SKIP_DIRS | RAW_DIRS, mask=True)
        project_guids, system_guids = classify_guids(all_guids)
        result["guid_count"] = len(project_guids)
        result["system_guid_count"] = len(system_guids)
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Auto-description generator
# ---------------------------------------------------------------------------

# Known FB patterns → human-readable description snippets (Chinese)
_FB_PATTERNS: dict[str, str] = {
    "FB_Machine": "SPT Machine state machine",
    "Machine": "SPT Machine state machine",
    "FB_ControlSource_HMI_Machine": "HMI + Production Monitor support",
    "FB_Unwind": "Unwind module",
    "FB_Sealer": "Sealer module",
    "FB_PullWheel": "PullWheel module",
    "FB_SealBar": "SealBar",
    "FB_Cylinder": "Cylinder control",
    "FB_EquipmentModuleTemplate": "EM template",
    "Axis_PTP_CoE": "CoE servo axis control",
    "Axis_IO": "IO axis control",
    "FB_Home_ByLimit": "Home by limit switch",
    "ServoDriver_DI": "Servo driver DI",
    "FB_ControlSource_HMI": "HMI data source",
}


def _generate_description(project_dir: Path, project_name: str,
                          analysis: dict) -> str:
    """Auto-generate a Chinese description from project code analysis.

    Reads MAIN POU, detects instantiated FBs, counts objects, and
    produces a concise one-line summary.
    """
    parts: list[str] = []

    # 1. Find and analyze MAIN POU
    plc_dirs = [d for d in project_dir.iterdir()
                if d.is_dir() and d.name not in SKIP_DIRS
                and d.name not in RAW_DIRS
                and not d.name.startswith("_")]

    fb_tags: set[str] = set()
    has_nc = False
    has_hmi = False
    main_comment = ""

    for plc_dir in plc_dirs:
        # Try to read MAIN
        for main_file in plc_dir.rglob("MAIN.TcPOU"):
            try:
                tree = ET.parse(main_file)
                root = tree.getroot()
                pou = root.find("POU")
                if pou is None:
                    continue

                # Declaration
                decl_el = pou.find("Declaration")
                decl_text = ""
                if decl_el is not None and decl_el.text:
                    txt = decl_el.text.strip()
                    if txt.startswith("<![CDATA[") and txt.endswith("]]>"):
                        decl_text = txt[9:-3]
                    else:
                        decl_text = txt

                # Implementation
                impl_el = pou.find("Implementation")
                impl_text = ""
                if impl_el is not None and len(impl_el):
                    lang_el = impl_el[0]
                    if lang_el.text:
                        txt = lang_el.text.strip()
                        if txt.startswith("<![CDATA[") and txt.endswith("]]>"):
                            impl_text = txt[9:-3]
                        else:
                            impl_text = txt

                # Detect FB instances
                for fb_name, tag in _FB_PATTERNS.items():
                    if fb_name in decl_text or fb_name in impl_text:
                        fb_tags.add(tag)

                # Detect NC
                if "Axis" in decl_text or "axis" in decl_text.lower() or "MC_" in impl_text:
                    has_nc = True

                # Detect HMI
                if "HMI" in decl_text or "ControlSource_HMI" in decl_text:
                    has_hmi = True

                # Extract first meaningful comment
                for line in impl_text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("//") and len(stripped) > 4:
                        comment = stripped[2:].strip()
                        # Skip boilerplate
                        skip = ("sample code", "provided by", "illustrative",
                                "without any", "warranties", "All sample")
                        if not any(k in comment.lower() for k in skip):
                            main_comment = comment[:60]
                            break

                break  # Only process first MAIN
            except Exception:
                continue
        if fb_tags:
            break

    # 2. Build description

    # Primary function from FB patterns
    if fb_tags:
        parts.append(" | ".join(sorted(fb_tags)))

    # Motion / NC
    if has_nc and "servo" not in " ".join(fb_tags).lower():
        parts.append("NC Motion Control")

    # HMI
    if has_hmi and "HMI" not in " ".join(fb_tags):
        parts.append("HMI Visualization")

    # Object counts
    obj_parts: list[str] = []
    pous = analysis.get("pou_count", 0)
    duts = analysis.get("dut_count", 0)
    gvls = analysis.get("gvl_count", 0)
    vis = analysis.get("vis_count", 0)

    if pous:
        obj_parts.append(f"{pous} POUs")
    if duts:
        obj_parts.append(f"{duts} DUTs")
    if gvls:
        obj_parts.append(f"{gvls} GVLs")
    if vis:
        obj_parts.append(f"{vis} VISUs")
    if obj_parts:
        parts.append(", ".join(obj_parts))

    # Libraries
    if analysis.get("has_libraries"):
        parts.append(f"{analysis.get('library_count', 0)} libs")

    # Fallback
    if not parts:
        parts.append(f"TwinCAT PLC project ({analysis.get('file_count', 0)} files)")

    return " | ".join(parts)
