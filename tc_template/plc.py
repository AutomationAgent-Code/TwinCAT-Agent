"""
PLC code operations — read/list POUs, DUTs, GVLs, VISUs.

File-system operations: list_objects, read_pou
PLCopen XML: pou_to_plcopen, plcopen_to_pou
COM operations (require win32com): create_pou, import_plcopen, build, library_mgmt
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# POU type → (SubType, XML tag, file extension, directory)
_POU_TYPES: dict[str, tuple[int, str, str, str]] = {
    "program":        (603, "POU", ".TcPOU", "POUs"),
    "functionBlock":  (604, "POU", ".TcPOU", "POUs"),
    "function":       (603, "POU", ".TcPOU", "POUs"),
    "struct":         (606, "DUT", ".TcDUT", "DUTs"),
    "enum":           (605, "DUT", ".TcDUT", "DUTs"),
    "union":          (607, "DUT", ".TcDUT", "DUTs"),
    "alias":          (623, "DUT", ".TcDUT", "DUTs"),
    "gvl":            (615, "GVL", ".TcGVL", "GVLs"),
    "interface":      (618, "Interface", ".TcIO", "Interfaces"),
}

# IEC language IDs for CreateChild vInfo
_IEC_LANGUAGES = {"ST": 6, "SFC": 0, "FBD": 5, "LD": 4, "CFC": 8, "IL": 2}


# Map file extensions to PLC object types
EXT_TYPE: dict[str, str] = {
    ".TcPOU": "POU",
    ".TcDUT": "DUT",
    ".TcGVL": "GVL",
    ".TcVIS": "VISU",
    ".TcGTLO": "TextList",
    ".TcVMO": "VisuManager",
    ".TcIO": "Interface",
}


def normalize_twincat_text(value: object) -> tuple[str, int]:
    """Return XML-safe TwinCAT source text and the replacement count.

    TwinCAT 4024 stores PLC objects as XML and rejects non-BMP characters in
    some Automation Interface paths.  A truncated emoji is even worse: its
    lone UTF-16 surrogate makes the whole ``.TcPOU`` impossible to serialize.
    Keep ordinary Unicode (including Chinese), tabs and line breaks, while
    replacing emoji, isolated surrogates and XML 1.0-invalid controls with a
    plain space so ST token boundaries remain valid.
    """
    text = str(value or "")
    output: list[str] = []
    replaced = 0
    index = 0
    while index < len(text):
        char = text[index]
        code = ord(char)
        if 0xD800 <= code <= 0xDBFF:
            # Python may receive a UTF-16 surrogate pair from COM/JSON rather
            # than one non-BMP scalar. Consume both halves as one replacement.
            if index + 1 < len(text) and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                index += 1
            output.append(" ")
            replaced += 1
        elif 0xDC00 <= code <= 0xDFFF or code > 0xFFFF:
            output.append(" ")
            replaced += 1
        elif code in (0xFFFE, 0xFFFF) or (
            code < 0x20 and char not in "\t\n\r"
        ):
            output.append(" ")
            replaced += 1
        else:
            output.append(char)
        index += 1
    return "".join(output), replaced


_DUT_ITEM_TYPES = {
    "enum": 605,
    "struct": 606,
    "union": 607,
    "alias": 623,
}


def normalize_dut_declaration(pou_type: str, name: str, declaration: str) -> str:
    """Return a complete TwinCAT ``TYPE ... END_TYPE`` declaration.

    Models and users commonly provide only the editor body (``STRUCT ...
    END_STRUCT`` or ``(...)``). Passing that shorthand as ``CreateChild``
    vInfo makes XAE 15 materialize item type 623 (Alias), even when subtype
    605/606/607 was requested. Canonicalize the common shorthand forms before
    the first COM call and reject a mismatched explicit type name.
    """
    kind = str(pou_type or "").casefold()
    if kind not in _DUT_ITEM_TYPES:
        return str(declaration or "")
    source = str(declaration or "").strip()
    if not source:
        raise ValueError(f"{kind} DUT '{name}' requires a declaration")

    prefix = ""
    type_start = re.search(r"(?im)^\s*TYPE\b", source)
    explicit = re.match(
        r"(?is)^\s*TYPE\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*END_TYPE\s*;?\s*$",
        source[type_start.start():] if type_start else source,
    )
    if explicit:
        declared_name, body = explicit.groups()
        if declared_name.casefold() != str(name).casefold():
            raise ValueError(
                f"DUT declaration name '{declared_name}' does not match object name '{name}'"
            )
        prefix = source[:type_start.start()].strip() if type_start else ""
        source = body.strip()
    elif type_start:
        raise ValueError(
            f"DUT '{name}' declaration must be a complete TYPE ... END_TYPE block"
        )

    if kind == "struct":
        if not re.match(r"(?is)^STRUCT\b", source) or not re.search(
            r"(?is)\bEND_STRUCT\s*;?\s*$", source
        ):
            raise ValueError(f"Struct DUT '{name}' must contain STRUCT ... END_STRUCT")
    elif kind == "union":
        if not re.match(r"(?is)^UNION\b", source) or not re.search(
            r"(?is)\bEND_UNION\s*;?\s*$", source
        ):
            raise ValueError(f"Union DUT '{name}' must contain UNION ... END_UNION")
    elif kind == "enum":
        enum_tail = re.search(
            r"\)\s*([A-Za-z_][A-Za-z0-9_]*)?\s*;?\s*$", source
        )
        if not source.startswith("(") or not enum_tail:
            raise ValueError(f"Enum DUT '{name}' must contain an '(...)' member list ending with );. "
                             "Place {attribute 'strict'} before TYPE; never append <strict> after the list.")
        base_type = enum_tail.group(1) or ""
        source = source[:enum_tail.start()] + ")" + (
            f" {base_type}" if base_type else ""
        ) + ";"
    else:
        source = source.rstrip().rstrip(";").rstrip() + ";"

    canonical = f"TYPE {name} :\n{source}\nEND_TYPE"
    return f"{prefix}\n{canonical}" if prefix else canonical


def validate_interface_declaration(name: str, declaration: str) -> str:
    """Reject inline members that TwinCAT requires as interface child nodes."""
    source = str(declaration or "")
    if re.search(r"(?im)^\s*(?:METHOD|PROPERTY|END_INTERFACE)\b", source):
        raise ValueError(
            f"Interface '{name}' declaration may only contain its INTERFACE header; "
            "create methods/properties with plc_create_member."
        )
    return source


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_objects(project_dir: str | Path) -> list[dict[str, Any]]:
    """Walk a PLC project directory and list all objects.

    Args:
        project_dir: Path to the PLC directory (e.g. ``G:/Prg/MyProject/PLC1``).

    Returns:
        List of ``{name, type, file_type, path, rel_path}`` sorted by type then name.
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return []

    objects: list[dict] = []

    for filepath in sorted(project_dir.rglob("*")):
        if not filepath.is_file():
            continue
        if filepath.name in (".git", ".vs", "_CompileInfo", "_Libraries"):
            continue
        ext = filepath.suffix
        if ext not in EXT_TYPE:
            continue

        # Extract name from XML (rather than relying on filename)
        name = _extract_name(filepath)

        obj_type = _extract_object_type(filepath, ext)

        objects.append({
            "name": name,
            "type": obj_type,
            "file_type": EXT_TYPE[ext],
            "path": str(filepath),
            "rel_path": str(filepath.relative_to(project_dir)),
        })

    objects.sort(key=lambda o: (o["type"], o["name"]))
    return objects


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_pou(project_dir: str | Path, pou_name: str) -> dict[str, Any]:
    """Read a PLC object's code from its file.

    Finds the file by matching ``pou_name`` against the XML ``Name``
    attribute, not the filename.

    Args:
        project_dir: Path to the PLC directory.
        pou_name: Case-insensitive POU/DUT/GVL name (e.g. ``MAIN``, ``ST_Descriptor2``).

    Returns:
        Dict with ``name``, ``type``, ``declaration``, ``implementation``,
        ``language``, ``methods``, ``path``.
    """
    project_dir = Path(project_dir)
    filepath = _find_object(project_dir, pou_name)
    if filepath is None:
        raise FileNotFoundError(f"Object '{pou_name}' not found in {project_dir}")

    return _parse_file(filepath)


def _find_object(project_dir: Path, name: str) -> Path | None:
    """Find the file containing an object by XML name."""
    name_lower = name.lower()
    for ext in EXT_TYPE:
        for filepath in project_dir.rglob(f"*{ext}"):
            try:
                obj_name = _extract_name(filepath).lower()
                if obj_name == name_lower:
                    return filepath
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# PLCopen XML (file-based)
# ---------------------------------------------------------------------------

def pou_to_plcopen(project_dir: str | Path, pou_name: str) -> str:
    """Export a POU as PLCopen XML string."""
    info = read_pou(project_dir, pou_name)
    info["declaration"], _ = normalize_twincat_text(info.get("declaration", ""))
    info["implementation"], _ = normalize_twincat_text(info.get("implementation", ""))
    # Build minimal PLCopen XML
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<pou xmlns="http://www.plcopen.org/xml/tc6_0201"',
        f'     name="{info["name"]}" pouType="{info["type"].lower()}">',
    ]
    if info.get("declaration"):
        lines.append("  <interface>")
        lines.append(f"    <body><![CDATA[{info['declaration']}]]></body>")
        lines.append("  </interface>")
    if info.get("implementation"):
        lang = info.get("language", "ST")
        lines.append("  <body>")
        lines.append(f"    <{lang}><![CDATA[{info['implementation']}]]></{lang}>")
        lines.append("  </body>")
    lines.append("</pou>")
    return "\n".join(lines)


def plcopen_to_pou(project_dir: str | Path, xml_str: str) -> str:
    """Import a PLCopen XML string, creating a new POU file.

    Returns the created POU name.
    """
    root = ET.fromstring(xml_str)
    ns = "http://www.plcopen.org/xml/tc6_0201"
    name = root.get("name", "ImportedPOU")
    pou_type = root.get("pouType", "functionBlock")

    decl = ""
    impl = ""
    language = "ST"

    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "body":
            if el.text and el.text.strip():
                decl = el.text.strip()
            for child in el:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag in ("ST", "SFC", "FBD", "LD", "IL", "CFC"):
                    language = ctag
                    impl = (child.text or "").strip()
        elif tag == "interface" and el.text and el.text.strip():
            decl = el.text.strip()

    decl, _ = normalize_twincat_text(decl)
    impl, _ = normalize_twincat_text(impl)

    # Build TcPlcObject XML
    pou_tag = "POU" if pou_type in ("program", "functionBlock", "function") else pou_type.upper()
    special = "None"
    if pou_type == "program":
        special = "None"
    elif pou_type == "function":
        special = "Function"

    # Determine extension and directory
    if pou_type in ("program", "functionBlock", "function"):
        ext = ".TcPOU"
        subdir = "POUs"
        xml_tag = "POU"
    elif pou_type in ("struct", "enum", "union", "alias"):
        ext = ".TcDUT"
        subdir = "DUTs"
        xml_tag = "DUT"
    elif pou_type == "globalVars":
        ext = ".TcGVL"
        subdir = "GVLs"
        xml_tag = "GVL"
    else:
        ext = ".TcPOU"
        subdir = "POUs"
        xml_tag = "POU"

    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<TcPlcObject Version="1.1.0.1">',
        f'  <{xml_tag} Name="{name}" Id="{{NEW_GUID}}" SpecialFunc="{special}">',
        f'    <Declaration><![CDATA[{decl}]]></Declaration>',
    ]
    if impl:
        xml_lines.append("    <Implementation>")
        xml_lines.append(f'      <{language}><![CDATA[{impl}]]></{language}>')
        xml_lines.append("    </Implementation>")
    xml_lines.append(f"  </{xml_tag}>")
    xml_lines.append("</TcPlcObject>")

    # Write file
    out_dir = Path(project_dir) / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}{ext}"
    out_path.write_text("\n".join(xml_lines), encoding="utf-8")

    return name


# ======================================================================
# Internal helpers
# ======================================================================

def _extract_name(filepath: Path) -> str:
    """Extract object name from XML file."""
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        for tag in ("POU", "DUT", "GVL", "Interface"):
            el = root.find(tag)
            if el is not None:
                return el.get("Name", filepath.stem)
    except Exception:
        pass
    return filepath.stem


def _extract_object_type(filepath: Path, ext: str) -> str:
    """Determine the specific type (Program, FunctionBlock, Struct, etc.)."""
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        for tag in ("POU", "DUT", "GVL", "Interface"):
            el = root.find(tag)
            if el is not None:
                if tag == "POU":
                    sf = el.get("SpecialFunc", "")
                    if sf == "Function":
                        return "Function"
                    return "FunctionBlock"  # default POU = FB in TwinCAT convention
                if tag == "GVL":
                    return "GlobalVars"
                if tag == "DUT":
                    # Read declaration to determine struct/enum/union/alias
                    decl_el = el.find("Declaration")
                    if decl_el is not None and decl_el.text:
                        text = decl_el.text.strip()
                        if "ENUM" in text:
                            return "Enum"
                        if "UNION" in text:
                            return "Union"
                        if "STRUCT" in text:
                            return "Struct"
                    return "DUT"
                return tag
    except Exception:
        pass
    return ext.replace(".", "")


def _parse_file(filepath: Path) -> dict[str, Any]:
    """Parse a TcPOU/TcDUT/TcGVL file and return structured data."""
    tree = ET.parse(filepath)
    root = tree.getroot()

    result: dict = {
        "name": "",
        "type": "",
        "declaration": "",
        "implementation": "",
        "language": "ST",
        "methods": [],
        "path": str(filepath),
    }

    for tag in ("POU", "DUT", "GVL", "Interface"):
        el = root.find(tag)
        if el is not None:
            result["name"] = el.get("Name", "")
            result["type"] = _extract_object_type(filepath, Path(filepath).suffix)

            # Declaration
            decl_el = el.find("Declaration")
            if decl_el is not None and decl_el.text:
                result["declaration"] = _unwrap_cdata(decl_el.text)

            # Implementation
            impl_el = el.find("Implementation")
            if impl_el is not None:
                if len(impl_el) > 0:
                    lang_el = impl_el[0]
                    result["language"] = lang_el.tag
                    if lang_el.text:
                        result["implementation"] = _unwrap_cdata(lang_el.text)

            # Sub-elements (Methods, Actions, Properties, Transitions)
            for child in el:
                if child.tag in ("Method", "Action", "Property", "Transition"):
                    m_decl = ""
                    m_impl = ""
                    m_decl_el = child.find("Declaration")
                    if m_decl_el is not None and m_decl_el.text:
                        m_decl = _unwrap_cdata(m_decl_el.text)
                    m_impl_el = child.find("Implementation")
                    if m_impl_el is not None and len(m_impl_el) > 0:
                        if m_impl_el[0].text:
                            m_impl = _unwrap_cdata(m_impl_el[0].text)
                    result["methods"].append({
                        "name": child.get("Name", ""),
                        "type": child.tag,
                        "declaration": m_decl,
                        "implementation": m_impl,
                    })
            break

    return result


def _unwrap_cdata(text: str) -> str:
    """Extract content inside CDATA wrapper."""
    text = text.strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        return text[9:-3]
    return text


# ======================================================================
# COM-based operations (require win32com + running TcXaeShell)
# ======================================================================

import time as _time

_POU_TYPES_COM = {
    'program': (602, 'POU', '.TcPOU', 'POUs'),
    'functionBlock': (604, 'POU', '.TcPOU', 'POUs'),
    'fb': (604, 'POU', '.TcPOU', 'POUs'),          # alias for functionBlock
    'function': (603, 'POU', '.TcPOU', 'POUs'),
    'struct': (606, 'DUT', '.TcDUT', 'DUTs'),
    'enum': (605, 'DUT', '.TcDUT', 'DUTs'),
    'union': (607, 'DUT', '.TcDUT', 'DUTs'),
    'alias': (623, 'DUT', '.TcDUT', 'DUTs'),
    'gvl': (615, 'GVL', '.TcGVL', 'GVLs'),
    'visu': (619, 'VISU', '.TcVIS', 'VISUs'),
    'interface': (618, 'Interface', '.TcIO', 'Interfaces'),
}

# ------------------------------------------------------------------
# COM helpers
# ------------------------------------------------------------------

def _cast_to(obj, interface_name: str):
    """Access TwinCAT COM interface properties via IDispatch dynamic dispatch.

    IMPORTANT: Do NOT use win32com.client.CastTo here — it corrupts the
    TwinCAT COM server's vtable state, causing permanent segfaults.
    ITcPlcDeclaration/ITcPlcImplementation are dual interfaces that expose
    DeclarationText/ImplementationText through IDispatch directly.

    The ``interface_name`` parameter is kept for API compatibility but
    is unused — the raw Dispatch object resolves property names correctly.
    """
    # Return raw Dispatch — IDispatch::GetIDsOfNames resolves
    # DeclarationText / ImplementationText at runtime, no vtable involved.
    return obj


def _find_nested_project(sysman):
    """Return the NestedProject COM tree item (contains POUs/DUTs/GVLs)."""
    plc = sysman.LookupTreeItem('TIPC')
    for child in plc or []:
        nested = getattr(child, 'NestedProject', None)
        if nested is not None:
            return nested
    raise RuntimeError('No NestedProject found in solution. Open a PLC project first.')


def _find_object_in_folder(folder, name: str):
    """Iterate a COM folder looking for an item by Name (case-insensitive)."""
    name_lower = name.lower()
    for item in folder or []:
        try:
            item_name = getattr(item, 'Name', '')
            if item_name.lower() == name_lower:
                return item
        except Exception:
            continue
    return None


def _get_nested_base_path(sysman):
    """Return path string for NestedProject, e.g. 'TIPC^PLC1^PLC1 Project'."""
    plc = sysman.LookupTreeItem('TIPC')
    for child in plc or []:
        nested = getattr(child, 'NestedProject', None)
        if nested is not None:
            nested_name = getattr(nested, 'Name', '')
            return f'TIPC^{child.Name}^{nested_name}'
    return ''


# ------------------------------------------------------------------
# COM POU code read / write
# (ITcPlcDeclaration.DeclarationText / ITcPlcImplementation.ImplementationText)
# ------------------------------------------------------------------

def com_list_objects() -> list[dict]:
    """List all PLC objects in the currently open project via COM tree iteration.

    No filesystem access — reads directly from TwinCAT IDE tree via COM.
    Returns list of ``{name, type, path}`` sorted by type then name.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    result: list[dict] = []

    folder_types = {
        'POUs': 'FunctionBlock', 'DUTs': 'DUT', 'GVLs': 'GlobalVars',
        'Interfaces': 'Interface', 'VISUs': 'VISU',
    }

    for folder_name, default_type in folder_types.items():
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        for item in folder or []:
            try:
                item_name = getattr(item, 'Name', '')
                item_path = getattr(item, 'PathName', '')
            except Exception:
                continue
            if not item_name:
                continue

            obj_type = default_type
            if folder_name == 'POUs':
                if item_name.upper().startswith('FB_'):
                    obj_type = 'FunctionBlock'
                elif item_name.upper().startswith('PRG_'):
                    obj_type = 'Program'
                elif item_name.upper().startswith('FUN_'):
                    obj_type = 'Function'
            elif folder_name == 'DUTs':
                obj_type = 'DUT'

            result.append({
                'name': item_name,
                'type': obj_type,
                'path': item_path or f'{_get_nested_base_path(sysman)}\\{folder_name}\\{item_name}',
            })

    result.sort(key=lambda o: (o['type'], o['name']))
    return result


def com_read_pou(name: str) -> dict:
    """Read a PLC object's code via COM interfaces.

    Searches POUs, DUTs, GVLs, and Interfaces folders for a matching Name.
    Uses ITcPlcDeclaration.DeclarationText and
    ITcPlcImplementation.ImplementationText.

    Returns:
        ``{name, type, declaration, implementation, language, methods, path}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    item = None
    found_folder = ''

    for folder_name in ('POUs', 'DUTs', 'GVLs', 'Interfaces'):
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        candidate = _find_object_in_folder(folder, name)
        if candidate is not None:
            item = candidate
            found_folder = folder_name
            break

    if item is None:
        raise FileNotFoundError(f"Object '{name}' not found in any PLC folder via COM")

    result: dict = {
        'name': getattr(item, 'Name', name),
        'type': found_folder,
        'declaration': '',
        'implementation': '',
        'language': 'ST',
        'methods': [],
        'path': getattr(item, 'PathName', ''),
    }

    # Refine POU type
    if found_folder == 'POUs':
        item_name = result['name']
        if item_name.upper().startswith('FB_'):
            result['type'] = 'FunctionBlock'
        elif item_name.upper().startswith('PRG_'):
            result['type'] = 'Program'
        elif item_name.upper().startswith('FUN_'):
            result['type'] = 'Function'
        else:
            result['type'] = 'FunctionBlock'
    elif found_folder == 'DUTs':
        result['type'] = 'DUT'
    elif found_folder == 'GVLs':
        result['type'] = 'GlobalVars'

    # Read declaration via COM
    try:
        decl = _cast_to(item, 'ITcPlcDeclaration')
        raw = getattr(decl, 'DeclarationText', '')
        if callable(raw):
            raw = raw()
        result['declaration'] = (raw or '').strip()
    except Exception:
        pass

    # Read implementation via COM (only POUs have implementations)
    if found_folder == 'POUs':
        try:
            impl = _cast_to(item, 'ITcPlcImplementation')
            raw = getattr(impl, 'ImplementationText', '')
            if callable(raw):
                raw = raw()
            result['implementation'] = (raw or '').strip()
        except Exception:
            pass

    # Scan child methods / actions / properties
    for child in item or []:
        try:
            child_name = getattr(child, 'Name', '')
            if not child_name:
                continue
        except Exception:
            continue

        m_decl = ''
        m_impl = ''
        try:
            cdecl = _cast_to(child, 'ITcPlcDeclaration')
            raw = getattr(cdecl, 'DeclarationText', '')
            if callable(raw):
                raw = raw()
            m_decl = (raw or '').strip()
        except Exception:
            pass
        try:
            cimpl = _cast_to(child, 'ITcPlcImplementation')
            raw = getattr(cimpl, 'ImplementationText', '')
            if callable(raw):
                raw = raw()
            m_impl = (raw or '').strip()
        except Exception:
            pass

        if not m_decl and not m_impl:
            continue

        result['methods'].append({
            'name': child_name,
            'type': 'Method',
            'declaration': m_decl,
            'implementation': m_impl,
        })

    return result


def com_write_pou(name: str, code: str, area: str = 'implementation',
                  method_name: str = None) -> dict:
    """Write code to a PLC object via COM.

    Uses ITcPlcDeclaration.DeclarationText or
    ITcPlcImplementation.ImplementationText — no filesystem, no dialog.

    Args:
        name: POU/DUT/GVL name (case-insensitive).
        code: New code text (replaces entire area).
        area: ``'declaration'`` or ``'implementation'``.
        method_name: If set, write to a specific Method/Action/Property child.

    Returns:
        ``{name, area, status}``.
    """
    code, sanitized_chars = normalize_twincat_text(code)
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    item = None
    for folder_name in ('POUs', 'DUTs', 'GVLs', 'Interfaces'):
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        candidate = _find_object_in_folder(folder, name)
        if candidate is not None:
            item = candidate
            break

    if item is None:
        raise FileNotFoundError(f"Object '{name}' not found in any PLC folder via COM")

    target = item
    if method_name:
        found_method = _find_object_in_folder(item, method_name)
        if found_method is None:
            raise FileNotFoundError(
                f"Method/Action/Property '{method_name}' not found in '{name}'"
            )
        target = found_method

    if area == 'declaration':
        decl = _cast_to(target, 'ITcPlcDeclaration')
        try:
            decl.DeclarationText = code
        except Exception:
            setattr(decl, 'DeclarationText', code)
    elif area == 'implementation':
        impl = _cast_to(target, 'ITcPlcImplementation')
        try:
            impl.ImplementationText = code
        except Exception:
            setattr(impl, 'ImplementationText', code)
    else:
        raise ValueError(f"area must be 'declaration' or 'implementation', got '{area}'")

    return {
        'name': name,
        'area': area,
        'status': 'written',
        'sanitized_chars': sanitized_chars,
    }



def _com_dte():
    from ._com import get_active_dte
    return get_active_dte()


# Localized "PLC Project" suffix across TwinCAT IDE languages (EN/DE/FR/ZH/…).
# A Chinese IDE names the nested project "<name>项目", so a bare "Project" check
# misses it. Keep EN first so NC/English projects behave exactly as before.
_PLC_PROJECT_MARKERS = ("Project", "Projekt", "Projet", "项目", "項目", "專案", "Progetto", "Proyecto")


def _is_plc_project_name(name) -> bool:
    n = name or ""
    return any(m in n for m in _PLC_PROJECT_MARKERS)


def _com_plc_path(dte):
    sysman = dte.Solution.Projects.Item(1).Object
    plc = sysman.LookupTreeItem('TIPC')
    for child in plc or []:
        try:
            # Pattern 1: NestedProject (e.g. TIPC^PLC^PLC Project / …^项目)
            # Locale-robust: EN "Project", ZH "项目", DE "Projekt", … — matching
            # only "Project" fails on a Chinese-localized IDE (node "<name>项目").
            nested = getattr(child, 'NestedProject', None)
            if nested and _is_plc_project_name(getattr(nested, 'Name', '')):
                return f'TIPC^{child.Name}^{nested.Name}'
        except Exception:
            pass
        # Pattern 2: Instance sub-child (e.g. TIPC^PLC1^PLC1 Instance)
        for sub in child or []:
            try:
                sub_name = getattr(sub, 'Name', '')
                if 'Instance' in sub_name:
                    return f'TIPC^{child.Name}^{sub_name}'
            except Exception:
                continue
    return ''


def create_pou(pou_type: str, name: str, *, declaration: str = '',
               implementation: str = '', language: str = 'ST',
               return_type: str = '', parent_path: str = '') -> dict:
    """Create a POU/DUT/GVL via COM CreateChild, then write code via COM properties.

    vInfo values per TwinCAT spec:
      Program/FB: language string e.g. 'ST', 'SFC'; Function: a two-element
      array ``[language, return_type]``.
      DUT types (Struct, Enum, Union):   '' (empty string)
      GVL:                                None
    """
    from .creation_source import declaration_for_creation
    declaration = declaration_for_creation(pou_type, name, declaration, return_type)
    declaration, declaration_replaced = normalize_twincat_text(declaration)
    implementation, implementation_replaced = normalize_twincat_text(implementation)
    if pou_type in _DUT_ITEM_TYPES:
        declaration = normalize_dut_declaration(pou_type, name, declaration)
    elif pou_type == "interface":
        declaration = validate_interface_declaration(name, declaration)
    sanitized_chars = declaration_replaced + implementation_replaced
    info = _POU_TYPES_COM.get(pou_type)
    if not info: raise ValueError(f'Unknown type: {pou_type}')
    sub_type, _, _, folder_name = info

    dte = _com_dte()
    dte.MainWindow.Visible = True
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)
    base = _get_nested_base_path(sysman)
    if not base: raise RuntimeError('No PLC project found in solution')

    if parent_path:
        if not parent_path.casefold().startswith((base + '^').casefold()):
            raise ValueError(f'Parent tree path is outside the current PLC project: {parent_path}')
        try:
            target_folder = sysman.LookupTreeItem(parent_path)
        except Exception as exc:
            raise FileNotFoundError(
                f'PLC parent folder could not be resolved: {parent_path}. '
                'No object was created. Inspect the live structure and create '
                'the intended folder explicitly before retrying; do not guess a substitute path.'
            ) from exc
        target_type = getattr(target_folder, 'ItemType', 0)
        target_type = target_type() if callable(target_type) else target_type
        if int(target_type or 0) != 601:
            raise ValueError(f'Parent tree path is not a PLC folder: {parent_path}')
        target_path = parent_path
    else:
        target_folder = _find_object_in_folder(nested, folder_name)
        if target_folder is None:
            target_folder = nested.CreateChild(folder_name, 601, '', None)
            _time.sleep(0.3)
        target_path = f'{base}^{folder_name}'

    # vInfo decided by object category (via sub_type), so aliases like 'fb'
    # resolve correctly regardless of the pou_type spelling.
    if sub_type in (602, 604):
        # Program / FunctionBlock: vInfo is a plain IEC language string.
        v_info = language
    elif sub_type == 603:
        # Function: unlike a FB/program, TwinCAT requires both the IEC
        # language and return type in a two-element SAFEARRAY.  Passing only
        # the return type creates a BSTR VARIANT, which XAE rejects.
        v_info = [language, return_type or 'BOOL']
    elif sub_type in (605, 606, 607, 623):
        # XAE materializes a DUT's concrete subtype from its initial
        # declaration.  Creating it empty and assigning DeclarationText later
        # turns Struct/Enum/Union into an Alias (623) on XAE 15.
        v_info = declaration or ''
    elif sub_type == 618:
        # Interface vInfo is the extended-interface type; empty means none.
        v_info = ''
    else:
        # A GVL may likewise receive its initial declaration via vInfo;
        # VISU still requires None.
        v_info = declaration if sub_type == 615 and declaration else None

    target_folder.CreateChild(name, sub_type, '', v_info)
    _time.sleep(0.5)

    pou_item = sysman.LookupTreeItem(f'{target_path}^{name}')
    if sub_type in _DUT_ITEM_TYPES.values():
        actual_type = getattr(pou_item, 'ItemType', None) if pou_item is not None else None
        actual_type = actual_type() if callable(actual_type) else actual_type
        try:
            actual_type = int(actual_type)
        except (TypeError, ValueError):
            actual_type = None
        if actual_type != sub_type:
            rolled_back = False
            try:
                target_folder.DeleteChild(getattr(pou_item, 'Name', name))
                rolled_back = True
            except Exception:
                pass
            rollback_note = "invalid object was rolled back" if rolled_back else "manual cleanup required"
            raise RuntimeError(
                f"TwinCAT created DUT '{name}' as itemType {actual_type}, expected {sub_type}; "
                f"{rollback_note}. Ensure the declaration is a complete TYPE ... END_TYPE block."
            )

    if declaration or implementation:
        if pou_item is not None:
            if declaration and sub_type not in (605, 606, 607, 623, 615):
                try:
                    decl = _cast_to(pou_item, 'ITcPlcDeclaration')
                    decl.DeclarationText = declaration
                except Exception as e:
                    return {'name': name, 'type': pou_type,
                            'status': 'created (code write failed)',
                            'error': str(e)[:120]}
            if implementation:
                try:
                    impl = _cast_to(pou_item, 'ITcPlcImplementation')
                    impl.ImplementationText = implementation
                except Exception as e:
                    return {'name': name, 'type': pou_type,
                            'status': 'created (impl write failed)',
                            'error': str(e)[:120]}

    return {
        'name': name,
        'type': pou_type,
        'status': 'created',
        'sanitized_chars': sanitized_chars,
    }


# ------------------------------------------------------------------
# P0 gap-fill: create/delete PLC project, delete POU, list variables,
# project structure, code search  (parity with Beckhoff coAgent)
# ------------------------------------------------------------------

def com_create_plc_project(name: str = 'PLC1',
                           template: str = 'Standard PLC Template') -> dict:
    """Create a new PLC project under the TIPC (PLC) config node via COM.

    The ``basic`` template scaffolds only a bare TwinCAT shell with no PLC
    project — this adds one (with a default MAIN PRG) so code can be written.

    Args:
        name: PLC project name.
        template: PLC template name (``'Standard PLC Template'`` = empty PLC
            project with MAIN) or a full path to a ``.plcproj`` / ``.tpzip``.

    Returns:
        ``{name, status}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    plc = sysman.LookupTreeItem('TIPC')

    # Guard against duplicate name
    if _find_object_in_folder(plc, name) is not None:
        raise ValueError(f"PLC project '{name}' already exists")

    plc.CreateChild(name, 0, '', template)
    return {'name': name, 'status': 'created', 'template': template}


def _resolve_plc_project_disk_target(tsproj: Path, name: str) -> tuple[Path, Path]:
    """Resolve a PLC project's file and exclusive directory from ``.tsproj``.

    The directory is accepted only when it is a proper child of the TwinCAT
    system-project directory and contains no second ``.plcproj``.  This keeps
    project deletion from ever guessing a folder from the display name.
    """
    tsproj = tsproj.resolve()
    root = tsproj.parent
    document = ET.parse(tsproj)
    project_path = ""
    for plc_node in document.getroot().iter():
        if plc_node.tag.rsplit("}", 1)[-1] != "Plc":
            continue
        for node in list(plc_node):
            if (node.tag.rsplit("}", 1)[-1] == "Project"
                    and str(node.attrib.get("Name") or "") == name):
                project_path = str(node.attrib.get("PrjFilePath") or "")
                break
        if project_path:
            break
    if not project_path:
        raise FileNotFoundError(
            f"PLC project '{name}' has no PrjFilePath in {tsproj}"
        )
    project_file = (root / project_path).resolve()
    if project_file.suffix.lower() != ".plcproj":
        raise ValueError(f"Unsafe PLC project file type: {project_file}")
    try:
        project_file.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"PLC project path escapes the system project: {project_file}") from exc
    project_dir = project_file.parent
    if project_dir == root:
        raise ValueError(
            "PLC project file is stored directly beside the .tsproj; "
            "automatic directory deletion is unsafe"
        )
    if not project_file.is_file():
        raise FileNotFoundError(f"PLC project file not found: {project_file}")
    other_projects = [p for p in project_dir.glob("*.plcproj")
                      if p.resolve() != project_file]
    if other_projects:
        raise ValueError(
            f"PLC directory contains other projects and will not be deleted: {project_dir}"
        )
    return project_file, project_dir


def com_remove_plc_project(name: str) -> dict:
    """Remove a PLC project from TIPC while retaining its disk files."""
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    plc = sysman.LookupTreeItem('TIPC')
    if _find_object_in_folder(plc, name) is None:
        raise FileNotFoundError(f"PLC project '{name}' not found")
    plc.DeleteChild(name)
    return {'name': name, 'status': 'removed', 'files_deleted': False}


def com_delete_plc_project(name: str) -> dict:
    """Remove a PLC project from TIPC and delete its exclusive disk directory."""
    dte = _com_dte()
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    system_project = dte.Solution.Projects.Item(1)
    tsproj = Path(str(system_project.FullName or ""))
    project_file, project_dir = _resolve_plc_project_disk_target(tsproj, name)
    plc = system_project.Object.LookupTreeItem('TIPC')
    target = _find_object_in_folder(plc, name)
    if target is None:
        raise FileNotFoundError(f"PLC project '{name}' not found")
    plc.DeleteChild(getattr(target, 'Name', name))
    try:
        dte.ExecuteCommand("File.SaveAll")
    except Exception:
        pass
    try:
        shutil.rmtree(project_dir)
    except Exception as exc:
        raise RuntimeError(
            f"PLC project was removed from XAE, but its files could not be deleted: "
            f"{project_dir}: {exc}"
        ) from exc
    return {
        'name': name,
        'status': 'deleted',
        'files_deleted': True,
        'project_file': str(project_file),
        'deleted_directory': str(project_dir),
    }


def com_delete_pou(name: str) -> dict:
    """Delete a POU/DUT/GVL/Interface by name via COM ``DeleteChild``."""
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    for folder_name in ('POUs', 'DUTs', 'GVLs', 'Interfaces', 'VISUs'):
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        candidate = _find_object_in_folder(folder, name)
        if candidate is not None:
            # DeleteChild wants the exact stored name (case-sensitive)
            folder.DeleteChild(getattr(candidate, 'Name', name))
            return {'name': name, 'folder': folder_name, 'status': 'deleted'}

    raise FileNotFoundError(f"Object '{name}' not found in any PLC folder via COM")


# VAR-section keywords → scope label
_VAR_SECTIONS = {
    'VAR_INPUT': 'input', 'VAR_OUTPUT': 'output', 'VAR_IN_OUT': 'in_out',
    'VAR_GLOBAL': 'global', 'VAR_TEMP': 'temp', 'VAR_STAT': 'static',
    'VAR_INST': 'instance', 'VAR_EXTERNAL': 'external', 'VAR': 'local',
}


def _parse_variables(decl_text: str) -> list[dict]:
    """Extract ``{name, type, scope}`` entries from a declaration's VAR blocks.

    Strips comments, tracks the current VAR_* section, and matches
    ``name [AT %...] : TYPE`` lines (comma-separated names supported).
    """
    if not decl_text:
        return []
    # Strip block + line comments
    text = re.sub(r'\(\*.*?\*\)', '', decl_text, flags=re.S)
    text = re.sub(r'//[^\n]*', '', text)

    variables: list[dict] = []
    scope = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()

        # Section start (VAR, VAR_INPUT, ... optionally with CONSTANT/RETAIN/PERSISTENT)
        head = upper.split()[0] if upper.split() else ''
        if head in _VAR_SECTIONS and not upper.startswith('END_VAR'):
            scope = _VAR_SECTIONS[head]
            continue
        if upper.startswith('END_VAR'):
            scope = None
            continue
        if scope is None:
            continue

        # name[, name] [AT %...] : TYPE [:= ...] ;
        m = re.match(r'^([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*'
                     r'(?:AT\s+%[^:]+)?:\s*([^;]+?)\s*(?::=.*)?;?\s*$', line)
        if not m:
            continue
        names = [n.strip() for n in m.group(1).split(',')]
        vtype = m.group(2).strip()
        for n in names:
            variables.append({'name': n, 'type': vtype, 'scope': scope})
    return variables


def com_list_variables(pou_name: str | None = None) -> list[dict]:
    """List declared variables across the PLC project via COM.

    Parses the declaration of every POU/GVL (or just *pou_name* if given) and
    returns ``{pou, name, type, scope}`` entries.

    Returns:
        list of variable dicts, sorted by (pou, scope, name).
    """
    if pou_name:
        objs = [com_read_pou(pou_name)]
    else:
        objs = []
        for meta in com_list_objects():
            if meta['type'] in ('DUT',):
                continue  # DUT members aren't runtime variables
            try:
                objs.append(com_read_pou(meta['name']))
            except Exception:
                continue

    result: list[dict] = []
    for obj in objs:
        for var in _parse_variables(obj.get('declaration', '')):
            result.append({'pou': obj['name'], **var})
    result.sort(key=lambda v: (v['pou'], v['scope'], v['name']))
    return result


def com_plc_structure() -> dict:
    """Return the PLC project structure as a folder → objects tree via COM.

    Returns:
        ``{project, folders: {POUs:[{name,type,methods:[...]}], DUTs:[...],
        GVLs:[...], Interfaces:[...], VISUs:[...]}}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    folders: dict[str, list] = {}
    for folder_name in ('POUs', 'DUTs', 'GVLs', 'Interfaces', 'VISUs'):
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        entries: list[dict] = []
        for item in folder or []:
            item_name = getattr(item, 'Name', '')
            if not item_name:
                continue
            methods: list[str] = []
            if folder_name == 'POUs':
                for child in item or []:
                    cn = getattr(child, 'Name', '')
                    if cn:
                        methods.append(cn)
            entries.append({'name': item_name, 'methods': methods})
        if entries:
            folders[folder_name] = entries

    return {'project': getattr(nested, 'Name', ''), 'folders': folders}


def com_search_code(pattern: str, *, ignore_case: bool = True,
                    regex: bool = False) -> list[dict]:
    """Search all POU declarations + implementations for *pattern* via COM.

    Args:
        pattern: substring (default) or regular expression (``regex=True``).
        ignore_case: case-insensitive match (default True).
        regex: treat *pattern* as a regex.

    Returns:
        list of ``{pou, area, line, text}`` matches.
    """
    flags = re.IGNORECASE if ignore_case else 0
    if regex:
        matcher = re.compile(pattern, flags)
        def hit(s: str) -> bool: return matcher.search(s) is not None
    else:
        needle = pattern.lower() if ignore_case else pattern
        def hit(s: str) -> bool:
            return needle in (s.lower() if ignore_case else s)

    matches: list[dict] = []
    for meta in com_list_objects():
        try:
            obj = com_read_pou(meta['name'])
        except Exception:
            continue
        for area in ('declaration', 'implementation'):
            body = obj.get(area, '')
            if not body:
                continue
            for i, line in enumerate(body.splitlines(), 1):
                if hit(line):
                    matches.append({'pou': obj['name'], 'area': area,
                                    'line': i, 'text': line.strip()})
        # also search method bodies
        for m in obj.get('methods', []):
            for area in ('declaration', 'implementation'):
                body = m.get(area, '')
                if not body:
                    continue
                for i, line in enumerate(body.splitlines(), 1):
                    if hit(line):
                        matches.append({'pou': f"{obj['name']}.{m['name']}",
                                        'area': area, 'line': i,
                                        'text': line.strip()})
    return matches


def com_rename_object(old_name: str, new_name: str) -> dict:
    """Rename a POU/DUT/GVL/Interface via COM (sets ITcSmTreeItem.Name).

    Returns:
        ``{old, new, folder, status}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    nested = _find_nested_project(sysman)

    for folder_name in ('POUs', 'DUTs', 'GVLs', 'Interfaces', 'VISUs'):
        folder = _find_object_in_folder(nested, folder_name)
        if folder is None:
            continue
        candidate = _find_object_in_folder(folder, old_name)
        if candidate is not None:
            if _find_object_in_folder(folder, new_name) is not None:
                raise ValueError(f"'{new_name}' already exists in {folder_name}")
            candidate.Name = new_name
            return {'old': old_name, 'new': new_name,
                    'folder': folder_name, 'status': 'renamed'}

    raise FileNotFoundError(f"Object '{old_name}' not found in any PLC folder via COM")


def import_plcopen(xml_file: str, options: int = 0) -> dict:
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    path = _com_plc_path(dte)
    plc_proj = sysman.LookupTreeItem(path)
    plc_proj.PlcOpenImport(str(xml_file), options)
    return {'status': 'imported', 'file': xml_file}


def export_plcopen(xml_file: str, pou_names: list[str]) -> dict:
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    path = _com_plc_path(dte)
    plc_proj = sysman.LookupTreeItem(path)
    plc_proj.PlcOpenExport(str(xml_file), ';'.join(pou_names))
    return {'status': 'exported', 'file': xml_file}


def _get_refs_node(sysman):
    """Return the References COM node (IS ALSO ITcPlcLibraryManager via IDispatch)."""
    base = _get_nested_base_path(sysman)
    if not base:
        raise RuntimeError('No PLC project found')
    return sysman.LookupTreeItem(base + '^References')


def list_libraries() -> list[dict]:
    """List library references in the current PLC project.

    Returns list of ``{name}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    result = []
    try:
        for ref in refs or []:
            result.append({'name': ref.Name})
    except Exception:
        pass
    return result


def scan_installed_libraries() -> list[dict]:
    """Scan ALL installed libraries on the system via COM.

    Returns list of ``{name, library_name, version, distributor, display_name}``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)

    result = []
    libs = refs.ScanLibraries()
    for lib in libs or []:
        try:
            result.append({
                'name': lib.Name,
                'library_name': lib.LibraryName,
                'version': lib.Version,
                'distributor': lib.Distributor,
                'display_name': lib.DisplayName,
            })
        except Exception:
            pass
    return result


def com_add_library(name: str, version: str = '*', distributor: str = '') -> dict:
    """Add a library reference to the PLC project.

    Args:
        name: Library name, e.g. ``'Tc2_Standard'``.
        version: Version string, ``'*'`` for latest.
        distributor: Distributor name, e.g. ``'Beckhoff Automation GmbH'``.

    Can also use display name format: ``'Tc2_Math, * (Beckhoff Automation GmbH)'``.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)

    if distributor:
        refs.AddLibrary(name, version, distributor)
    else:
        refs.AddLibrary(name, version, '')

    return {'library': name, 'version': version, 'status': 'added'}


def com_remove_library(name: str, version: str = '', distributor: str = '') -> dict:
    """Remove a library reference from the PLC project.

    Args:
        name: Library name to remove.
        version: Version (empty = any).
        distributor: Distributor (empty = any).
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.RemoveReference(name, version, distributor)
    return {'library': name, 'status': 'removed'}


def com_add_placeholder(placeholder_name: str, default_lib: str = '',
                         default_version: str = '*',
                         default_distributor: str = '') -> dict:
    """Add a placeholder reference to the PLC project.

    Args:
        placeholder_name: Placeholder name, e.g. ``'Placeholder_NC'``.
        default_lib: Default library this placeholder resolves to.
        default_version: Default version (``'*'`` for latest).
        default_distributor: Default vendor name.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.AddPlaceholder(placeholder_name, default_lib,
                         default_version, default_distributor)
    return {'placeholder': placeholder_name, 'status': 'added'}


def com_freeze_placeholder(placeholder_name: str) -> dict:
    """Freeze a placeholder's version (pin to current resolution)."""
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.FreezePlaceholder(placeholder_name)
    return {'placeholder': placeholder_name, 'status': 'frozen'}


def com_insert_repository(name: str, root_folder: str, index: int = 0) -> dict:
    """Add a library repository.

    Args:
        name: Repository display name.
        root_folder: Path to the repository folder on disk.
        index: Insert position (0 = first).
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.InsertRepository(name, root_folder, index)
    return {'repository': name, 'path': root_folder, 'status': 'added'}


def com_remove_repository(name: str) -> dict:
    """Remove a library repository by name."""
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.RemoveRepository(name)
    return {'repository': name, 'status': 'removed'}


def com_install_library(repository_name: str, lib_path: str,
                         overwrite: bool = False) -> dict:
    """Install a library from a repository.

    Args:
        repository_name: Repository name.
        lib_path: Path to the .library file.
        overwrite: If True, overwrite existing installation.
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.InstallLibrary(repository_name, lib_path, overwrite)
    return {'repository': repository_name, 'path': lib_path, 'status': 'installed'}


def com_uninstall_library(repository_name: str, library_name: str,
                           version: str = '', distributor: str = '') -> dict:
    """Uninstall a library from a repository.

    Args:
        repository_name: Repository name.
        library_name: Library to uninstall.
        version: Version (empty = any).
        distributor: Distributor (empty = any).
    """
    dte = _com_dte()
    sysman = dte.Solution.Projects.Item(1).Object
    refs = _get_refs_node(sysman)
    refs.UninstallLibrary(repository_name, library_name, version, distributor)
    return {'library': library_name, 'status': 'uninstalled'}


def build_project() -> dict:
    from .inspect import clear_error_list, read_error_list
    dte = _com_dte()
    sln = dte.Solution

    clear_error_list(dte=dte)
    sln.SolutionBuild.Build()

    # Poll for build completion
    import time as _time
    deadline = _time.monotonic() + 30.0
    while _time.monotonic() < deadline:
        try:
            if dte.Solution.SolutionBuild.BuildState == 2:
                break
        except Exception:
            pass
        _time.sleep(1.0)

    r = read_error_list(dte=dte)
    return {'status': 'built', 'errors': r.get('errors', []),
            'error_count': r.get('error_count', 0)}
