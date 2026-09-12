"""
tc-template CLI — TwinCAT Project Template Manager.

Commands:
  list              List available templates
  info <name>       Show template details
  create <name>     Scaffold project, open in VS via COM, read error list
  add <path>        Extract template from existing TwinCAT project
  inspect           Read error list from open/running Visual Studio
  validate <name>   Validate a template
  remove <name>     Delete a template
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from . import __version__
from .models import TemplateMetadata, TemplateNotFoundError
from .repository import (
    list_templates, get_template, validate_template, remove_template,
)
from .extract import extract_template, analyze_project
from .scaffold import scaffold
from .plc import list_objects, read_pou, pou_to_plcopen, plcopen_to_pou
from .tc_platform import (
    build_solution, build_plc_project, check_all_objects,
    get_target_net_id, set_target_net_id,
    set_silent_mode, set_boot_project, deploy,
    list_static_routes, resolve_target, add_static_route,
    search_network_targets, list_local_adapters,
    scan_devices,
    list_io_variables, get_mapping_info, link_variable, unlink_variable,
    find_servo_drives, create_nc_task_and_axes,
    nc_structure, nc_axis_info, nc_axis_params, nc_set_axis_params,
    nc_create_task, nc_create_axis,
    find_nc_encoders, nc_link_drive, nc_link_encoder, nc_links,
    nc_state as get_nc_state, nc_axis_state, nc_axis_move, nc_validate,
    get_local_tc_version, get_target_tc_version,
    get_installed_tc_versions,
    get_pinned_tc_version, set_pin_tc_version,
    get_target_device_name,
    close_solution, open_solution, quit_xae,
    find_beckhoff_devices,
    get_build_platform, set_build_platform, list_build_platforms,
    list_projects, get_project_info, get_io_structure,
    read_device_settings, write_device_settings,
    get_ethercat_master_adapter, change_ethercat_master_adapter,
)
from ._ps_bridge import (
    com_system_structure, com_system_settings, com_system_settings_set,
    com_core_info, com_core_assign, com_realtime_info, com_realtime_validate,
    com_task_info, com_task_core_assign, com_task_settings_set,
    com_system_add, com_system_remove, com_realtime_refresh,
    com_safety_structure, com_safety_project_info, com_safety_files,
    com_safety_target_info, com_safety_aliases, com_safety_application, com_safety_logic_check,
    com_safety_validate,
    com_safety_import, com_safety_create, com_safety_export, com_safety_remove,
    com_safety_delete,
    com_get_build_platform, com_set_build_platform, com_list_build_platforms,
)


# ======================================================================
# Display helpers
# ======================================================================

def _table(ts: list[TemplateMetadata]) -> None:
    if not ts:
        click.echo("  (none)")
        return
    click.echo(f"  {'Name':<22} {'Ver':<8} {'Category':<10} Description")
    click.echo(f"  {'-'*22} {'-'*8} {'-'*10} {'-'*20}")
    for t in ts:
        click.echo(f"  {t.name:<22} {t.version:<8} {t.category:<10} "
                   f"{t.description[:30] if t.description else '-'}")


def _detail(meta: TemplateMetadata) -> None:
    click.echo(f"\n  {meta.display_name or meta.name}  ({meta.name}, v{meta.version})")
    click.echo(f"  Desc:  {meta.description or '-'}")
    click.echo(f"  GUID:  {meta.guid.strategy}")
    if meta.variables:
        click.echo(f"  Vars:  {', '.join(v.name for v in meta.variables)}")


# ======================================================================
# Inspect (COM-based, NOT pyautogui)
# ======================================================================

def _inspect(sln_path: str | None = None) -> None:
    """Connect to running TwinCAT, read Error Items. Never opens/closes solutions."""
    try:
        from .inspect import read_error_list
    except ImportError:
        click.echo("  (comtypes required: pip install comtypes)")
        return

    click.echo("  Connecting to Visual Studio...")
    r = read_error_list(wait_for_sln=sln_path)

    if not r["success"]:
        click.echo(f"  Error: {r.get('raw_text', 'unknown')[:300]}")
        return

    errors = r["errors"]
    if not errors:
        click.echo("  Error List: 0 items (clean)")
        return

    click.echo(f"  Error List: {len(errors)} items "
               f"({r['error_count']} err, {r['warning_count']} warn, {r['info_count']} info)")

    for sev, label in [("Error", "ERROR"), ("Warning", "WARNING"), ("Info", "INFO")]:
        group = [e for e in errors if e["severity"] == sev]
        if not group:
            continue
        click.echo(f"\n  [{label}]")
        for e in group:
            d = e.get("description", "")
            f = e.get("file", "")
            ln = e.get("line", "")
            loc = f" ({f}:{ln})" if f else ""
            click.echo(f"    {d}{loc}")


# ======================================================================
# CLI
# ======================================================================

@click.group()
@click.version_option(version=__version__, prog_name="tc-template")
def main():
    """TwinCAT PLC Project Template Manager."""


# ------------------------------------------------------------------
# list
# ------------------------------------------------------------------

@main.command("list")
@click.option("--category", "-c", default=None)
@click.option("--repo", "-r", default=None)
def cmd_list(category: str | None, repo: str | None):
    """List available templates."""
    templates = list_templates(category=category, repo_dir=repo)
    click.echo(f"\n  {len(templates)} template(s)\n")
    _table(templates)


# ------------------------------------------------------------------
# info
# ------------------------------------------------------------------

@main.command("info")
@click.argument("name")
@click.option("--repo", "-r", default=None)
def cmd_info(name: str, repo: str | None):
    """Show template details."""
    try:
        _detail(get_template(name, repo_dir=repo))
    except TemplateNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True); sys.exit(1)


# ------------------------------------------------------------------
# create  (scaffold → open via COM → inspect error list)
# ------------------------------------------------------------------

@main.command("create")
@click.argument("template", required=False)
@click.option("--name", "-n", default=None, help="New project name")
@click.option("--output", "-o", default=None, help="Output directory")
@click.option("--plc-name", default=None, help="PLC name (default: from template)")
@click.option("--tc-version", default=None, help="TwinCAT version (auto-detect)")
@click.option("--repo", "-r", default=None)
def cmd_create(template: str | None, name: str | None, output: str | None,
               plc_name: str | None, tc_version: str | None, repo: str | None):
    """Scaffold a project, open in VS via COM, read error list.

    \b
    tc-template create packml -n MyProject -o G:/Prj
    tc-template create                    (interactive)

    Before creating, shows the current target NetId and its TwinCAT version.
    The new project is generated with the matching TcVersion.
    """
    # ── show target & version info ──
    from .tc_platform import (get_active_target_netid, get_cached_target_version,
                                list_static_routes)
    netid = get_active_target_netid()
    routes = list_static_routes()

    # When COM returns IP.1.1 but the route table has the real NetId, use that
    if netid and routes:
        for route in routes:
            if route["address"] in netid or netid.startswith(route["address"]):
                if route["net_id"] != netid:
                    netid = route["net_id"]
                target_label = f"{route['name']} ({netid})"
                break
        else:
            target_label = netid
    elif netid:
        target_label = netid

    if netid:
        # Try to detect version: XAE project → cache → local fallback
        try:
            tcver = get_target_tc_version()
        except Exception:
            tcver = get_local_tc_version()
            tcver["source"] = "local"

        cached = get_cached_target_version(netid)
        if tcver.get("source") == "project":
            source_label = "from open project"
        elif cached:
            parts = cached.split(".")
            tcver = {
                "major": int(parts[0]), "minor": int(parts[1]),
                "build": int(parts[2]),
                "revision": int(parts[3]) if len(parts) > 3 else 0,
                "version_str": cached, "source": "cache",
            }
            source_label = "from cache"
        else:
            source_label = "local install (may differ from target)"
        click.echo(f"  Target: {target_label}")
        click.echo(f"  TwinCAT: {tcver['version_str']}  ({source_label})")
    else:
        click.echo("  (no target set, no XAE running)")
        tcver = get_local_tc_version()
        tcver["source"] = "local"
        click.echo(f"  Local TwinCAT: {tcver['version_str']}")

    # inputs
    if template is None:
        templates = list_templates(repo_dir=repo)
        if not templates:
            click.echo("[ERROR] No templates", err=True); sys.exit(1)
        click.echo("\nAvailable templates:")
        _table(templates)
        template = click.prompt("\nTemplate name", type=str)

    if name is None:
        # Auto-derive from controller name in route table, with date suffix
        default_name = None
        try:
            devname = get_target_device_name()
            if devname and devname != netid and not devname.startswith("169.254."):
                from datetime import datetime as _dt
                default_name = f"{devname}-{_dt.now().strftime('%Y%m%d%H%M')}"
        except Exception:
            pass
        if default_name:
            name = click.prompt("New project name", default=default_name, type=str)
        else:
            name = click.prompt("New project name", type=str)

    if output is None:
        output = click.prompt("Output directory", default=f"output/{name}", type=str)
    else:
        output = str(Path(output) / name)

    # Load template to get default PLC name
    if plc_name is None:
        try:
            meta = get_template(template, repo_dir=repo)
            for v in meta.variables:
                if v.name == "PLC_NAME":
                    plc_name = v.default
                    break
        except Exception:
            pass
        if not plc_name:
            plc_name = "PLC1"

    # Version selection
    # --tc-version flag → use directly (skip picker)
    # interactive + multiple versions → picker with recommendation
    # non-interactive / single version → auto-detect
    if tc_version is not None:
        # User explicitly specified --tc-version, use as-is
        pass
    else:
        installed = get_installed_tc_versions()
        if len(installed) > 1 and sys.stdin.isatty():
            click.echo(f"\n  ── Select TwinCAT Version ──")
            click.echo(f"  Available versions (local install):")
            # Default = target version if available, else active version
            default_idx = 0
            target_str = tcver.get("version_str", "")
            for i, v in enumerate(installed):
                markers = []
                if v["active"]:
                    markers.append("current")
                if v["version_str"] == target_str:
                    markers.append("matches target")
                    default_idx = i
                if not markers:
                    # If this matches the target, prefer it as default
                    pass
                label = f"  ← {' · '.join(markers)}" if markers else ""
                click.echo(f"    {i + 1}. {v['version_str']}{label}")
            # If no version matched the target, default to active
            if not any(v["version_str"] == target_str for v in installed):
                for i, v in enumerate(installed):
                    if v["active"]:
                        default_idx = i
                        break
            choice = click.prompt(
                f"  Select version",
                type=click.IntRange(1, len(installed)),
                default=default_idx + 1,
                show_default=True,
            )
            tc_version = installed[choice - 1]["version_str"]
        else:
            tc_version = tcver["version_str"]

    click.echo(f"\n  Template:   {template}")
    click.echo(f"  Project:    {name}")
    click.echo(f"  Output:     {output}")
    click.echo(f"  PLC:        {plc_name}")
    click.echo(f"  TcVersion:  {tc_version}")

    # scaffold
    try:
        result = scaffold(
            template_names=[template], output_dir=output,
            user_vars={
                "PROJECT_NAME": name,
                "PLC_NAME": plc_name,
                "TC_VERSION": tc_version,
            },
            interactive=False, dry_run=False, no_hooks=True,
            repo_dir=repo,
        )
    except Exception as e:
        click.echo(f"\n[ERROR] Scaffold failed: {e}", err=True)
        import traceback; traceback.print_exc(); sys.exit(1)

        click.echo(f"\n  Created: {result['files_created']} files, {result['guid_count']} GUIDs")

    # Patch .tsproj before opening (TcVersionFixed + TargetNetId)
    # This is safe because XAE hasn't opened the project yet
    tsproj_files = list(Path(result["output_dir"]).rglob("*.tsproj"))
    if tsproj_files:
        import re as _re
        tsproj = tsproj_files[0]
        tsproj_text = tsproj.read_text(encoding="utf-8", errors="replace")
        tsproj_text = _re.sub(
            r'TcVersion\s*=\s*"[^"]*"',
            f'TcVersion="{tc_version}" TcVersionFixed="true"',
            tsproj_text, count=1,
        )
        tsproj_text = _re.sub(
            r'(<Project\s+ProjectGUID="[^"]+")',
            f'\\1 TargetNetId="{netid}" Target64Bit="true"',
            tsproj_text, count=1,
        )
        tsproj.write_text(tsproj_text, encoding="utf-8")
        click.echo(f"  TcVersion: {tc_version} (fixed), Target: {netid}")

    # Open .sln via os.startfile (async — XAE registers in ROT later)
    sln_files = list(Path(result["output_dir"]).glob("*.sln"))
    sln = str(sln_files[0]) if sln_files else None
    if sln:
        import os as _os
        _os.startfile(sln)
        click.echo(f"  Opening: {sln}")
    click.echo()

    # Connect via _dte() which retries until .Solution is ready
    from .tc_platform import _dte as _platform_dte
    try:
        dte = _platform_dte()
        from .inspect import read_error_list
        r = read_error_list(dte=dte, wait_for_sln=sln)
        if not r["success"]:
            click.echo(f"  Error: {r.get('raw_text', 'unknown')[:300]}")
        else:
            errors = r["errors"]
            if not errors:
                click.echo("  Error List: 0 items (clean)")
            else:
                click.echo(f"  Error List: {len(errors)} items "
                           f"({r['error_count']} err, {r['warning_count']} warn, {r['info_count']} info)")
                for sev, label in [("Error", "ERROR"), ("Warning", "WARNING"), ("Info", "INFO")]:
                    group = [e for e in errors if e["severity"] == sev]
                    if not group: continue
                    click.echo(f"\n  [{label}]")
                    for e in group:
                        click.echo(f"    {e['description'][:200]}")
    except Exception:
        click.echo("  (could not connect to XAE — project created, check manually)")

# ------------------------------------------------------------------
# add
# ------------------------------------------------------------------

@main.command("add")
@click.argument("source", type=click.Path(exists=True))
@click.option("--name", "-n", default=None, help="Template name")
@click.option("--project-name", default=None, help="Original project name")
@click.option("--plc-name", default=None, help="Original PLC name")
@click.option("--repo", "-r", default=None)
def cmd_add(source: str, name: str | None, project_name: str | None,
            plc_name: str | None, repo: str | None):
    """Extract a template from an existing TwinCAT project.

    SOURCE is the project directory. Template stored in Repository/<name>/.
    """
    source_path = Path(source)
    click.echo(f"\n  Analyzing: {source_path}")
    info = analyze_project(source_path)

    if not info["is_twincat_project"]:
        click.echo("[ERROR] Not a TwinCAT project (need .sln + .tsproj)", err=True)
        sys.exit(1)

    click.echo(f"  Project: {info['detected_project_name']}")
    click.echo(f"  PLC:     {info['detected_plc_name']}")
    click.echo(f"  Files:   {info['file_count']}")
    click.echo(f"  GUIDs:   {info['guid_count']} project / {info['system_guid_count']} system")
    if info["has_libraries"]:
        click.echo(f"  Libs:    {info['library_count']} (_Libraries untouched)")

    if name is None:
        name = info["suggested_name"]
        click.echo(f"\n  Template name: {name}")
    if project_name is None:
        project_name = info["detected_project_name"]
    if plc_name is None:
        plc_name = info["detected_plc_name"]

    if not click.confirm(f"\n  Extract '{name}'?", default=True):
        click.echo("Cancelled"); return

    try:
        meta = extract_template(
            project_dir=source_path, template_name=name,
            project_name=project_name, plc_name=plc_name,
            auto_detect=True, repo_dir=repo,
        )
        click.echo(f"\n  Done: {meta.name} → Repository/{meta.name}/")
    except FileExistsError as e:
        click.echo(f"[ERROR] {e}", err=True); sys.exit(1)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)
        import traceback; traceback.print_exc(); sys.exit(1)


# ------------------------------------------------------------------
# inspect  (standalone — read errors from open VS)
# ------------------------------------------------------------------

@main.command("inspect")
def cmd_inspect():
    """Read error list from open Visual Studio (COM, no pyautogui)."""
    _inspect()


# ------------------------------------------------------------------
# validate
# ------------------------------------------------------------------

@main.command("validate")
@click.argument("name")
@click.option("--repo", "-r", default=None)
def cmd_validate(name: str, repo: str | None):
    ok, issues = validate_template(name, repo_dir=repo)
    if ok:
        click.echo(f"'{name}' is valid.")
    else:
        click.echo(f"'{name}' has issues:"); sys.exit(1)
    for i in issues:
        click.echo(f"  {i}")


# ------------------------------------------------------------------
# remove
# ------------------------------------------------------------------

@main.command("remove")
@click.argument("name")
@click.option("--force", "-f", is_flag=True)
@click.option("--repo", "-r", default=None)
def cmd_remove(name: str, force: bool, repo: str | None):
    if not force and not click.confirm(f"Delete '{name}' permanently?"):
        return
    try:
        remove_template(name, repo_dir=repo)
        click.echo(f"'{name}' removed.")
    except TemplateNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True); sys.exit(1)


# ======================================================================
# plc — PLC programming commands
# ======================================================================

@main.group("plc")
def cmd_plc():
    """PLC code operations: read, write, list, export, import."""


@cmd_plc.command("list")
def plc_list():
    """List all PLC objects via COM (requires running TcXaeShell)."""
    from . import _ps_bridge as ps
    try:
        objs = ps.com_list()
    except Exception as e:
        raise click.ClickException(str(e)) from e
    if not objs:
        click.echo("  (no PLC objects found)")
        return
    click.echo(f"\n  {len(objs)} object(s)\n")
    by_folder: dict[str, list] = {}
    for o in objs:
        by_folder.setdefault(o["folder"], []).append(o)
    for folder, items in sorted(by_folder.items()):
        click.echo(f"  [{folder}]")
        for o in items:
            click.echo(f"    {o['name']}")


@cmd_plc.command("read")
@click.argument("pou_name")
def plc_read(pou_name: str):
    """Read and display a PLC object's code via COM."""
    from . import _ps_bridge as ps
    try:
        info = ps.com_read_pou(pou_name)
    except Exception as e:
        raise click.ClickException(f"COM: {e}") from e

    click.echo(f"\n  {info['name']}")
    if info["declaration"]:
        click.echo(f"\n  --- Declaration ---")
        for line in info["declaration"].split("\n"):
            click.echo(f"  {line}")
    if info["implementation"]:
        click.echo(f"\n  --- Implementation ---")
        for line in info["implementation"].split("\n"):
            click.echo(f"  {line}")
    for m in info.get("methods") or []:
        click.echo(f"\n  --- Method: {m['name']} ---")
        if m["declaration"]:
            click.echo(f"  {m['declaration']}")
        if m["implementation"]:
            click.echo(f"  {m['implementation']}")



@cmd_plc.command("write")
@click.argument("pou_name")
@click.argument("code")
@click.option("--area", default="implementation", help="declaration or implementation")
@click.option("--method", default=None, help="Method/Action/Property name")
def plc_write(pou_name: str, code: str, area: str, method: str | None):
    """Write code to a PLC object via COM (no filesystem, no dialog)."""
    from . import _ps_bridge as ps
    try:
        ps.com_write_pou(pou_name, code, area=area, method_name=method or "")
        click.echo(f"  Written to {pou_name}.{method or area}")
    except Exception as e:
        raise click.ClickException(str(e)) from e



@cmd_plc.command("export")
@click.argument("project_dir", type=click.Path(exists=True))
@click.argument("pou_name")
@click.option("--output", "-o", default=None, help="Output XML file")
def plc_export(project_dir: str, pou_name: str, output: str | None):
    """Export a POU as PLCopen XML."""
    try:
        xml_str = pou_to_plcopen(project_dir, pou_name)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    if output:
        p = Path(output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(xml_str, encoding="utf-8")
        click.echo(f"  Exported to {output}")
    else:
        click.echo(xml_str)


@cmd_plc.command("import")
@click.argument("project_dir", type=click.Path(exists=True))
@click.argument("xml_file", type=click.Path(exists=True))
def plc_import(project_dir: str, xml_file: str):
    """Import a PLCopen XML file as a new POU."""
    xml_str = Path(xml_file).read_text(encoding="utf-8")
    try:
        name = plcopen_to_pou(project_dir, xml_str)
        click.echo(f"  Imported POU: {name}")
    except Exception as e:
        raise click.ClickException(str(e)) from e


# ======================================================================
# plc-com — COM-based PLC operations
# ======================================================================

@cmd_plc.command("create-com")
@click.argument("name")
@click.option("--type", "-t", "pou_type", default="functionBlock",
              help="POU type: program, functionBlock, function, struct, enum, gvl")
@click.option("--language", "-l", default="ST", help="IEC language: ST, SFC, FBD, LD, CFC")
@click.option("--decl", default=None, help="Declaration text (optional)")
@click.option("--impl", default=None, help="Implementation text (optional)")
def plc_create_com(name: str, pou_type: str, language: str, decl: str | None, impl: str | None):
    """Create a new PLC object via COM and optionally write code."""
    from . import _ps_bridge as ps
    alias = {"functionblock": "fb", "fb": "fb", "program": "program",
             "function": "function", "struct": "struct", "enum": "enum",
             "union": "union", "gvl": "gvl"}
    t = alias.get(pou_type.lower())
    if t is None:
        raise click.BadParameter(
            f"unsupported COM type {pou_type!r}; supported: "
            f"{', '.join(sorted(set(alias.values())))}",
            param_hint="--type",
        )
    try:
        msg = ps.com_new_pou(name, pou_type=t, declaration=decl or "",
                             implementation=impl or "", language=language)
        click.echo(f"  {msg}")
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_plc.command("create-fb")
@click.argument("name")
@click.option("--author", "-a", default="", help="Author (header comment)")
@click.option("--purpose", "-p", default="", help="One-line responsibility (header comment)")
@click.option("--no-enum", is_flag=True, help="Skip the companion E_<Name>State enum")
def plc_create_fb(name: str, author: str, purpose: str, no_enum: bool):
    """Create a STANDARD device-FB (状态四件套 + 中文头注释 + CASE 状态机).

    符合 docs/plc_coding_standard.md，配套生成 E_<Name>State 枚举。
    """
    from . import _ps_bridge as ps
    from .fb_scaffold import standard_fb
    sk = standard_fb(name, author=author, purpose=purpose)
    try:
        if not no_enum:
            ps.com_new_pou(sk["enum_name"], pou_type="enum",
                           declaration=sk["enum_declaration"], implementation="")
            click.echo(f"  Created enum: {sk['enum_name']}")
        ps.com_new_pou(sk["fb_name"], pou_type="fb",
                       declaration=sk["fb_declaration"],
                       implementation=sk["fb_implementation"])
        click.echo(f"  Created standard FB: {sk['fb_name']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("create-project")
@click.argument("name", default="PLC1")
@click.option("--template", default="Standard PLC Template",
              help="PLC template name or path to .plcproj/.tpzip")
def plc_create_project(name: str, template: str):
    """Create a new PLC project (with MAIN) under TIPC via COM."""
    from ._ps_bridge import com_create_plc_project
    try:
        r = com_create_plc_project(name, template)
        click.echo(f"  Created PLC project: {r['name']} ({r['status']})")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("delete-project")
@click.argument("name")
def plc_delete_project(name: str):
    """Delete a PLC project from XAE and remove its local directory."""
    from ._ps_bridge import com_delete_plc_project
    try:
        r = com_delete_plc_project(name)
        click.echo(f"  Deleted PLC project: {r['name']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("delete-pou")
@click.argument("name")
@click.option("--path", default="", help="Exact tree path from plc_find")
@click.option("--dry-run", is_flag=True, help="Preview references without deleting")
@click.option("--force", is_flag=True, help="Delete even when references are found")
def plc_delete_pou(name: str, path: str, dry_run: bool, force: bool):
    """Delete a POU/DUT/GVL/Interface by name via COM."""
    from ._ps_bridge import com_delete_pou
    try:
        r = com_delete_pou(name, path=path, dry_run=dry_run, force=force)
        click.echo(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("remove-project")
@click.argument("name")
def plc_remove_project(name: str):
    """Remove a PLC project from XAE while retaining local files."""
    from ._ps_bridge import com_remove_plc_project
    try:
        r = com_remove_plc_project(name)
        click.echo(f"  Removed PLC project (files retained): {r['name']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("delete-member")
@click.argument("pou")
@click.argument("name")
@click.option("--type", "member_type", default="method", show_default=True,
              type=click.Choice(["method", "property", "action", "transition",
                                 "propget", "propset"]))
@click.option("--path", default="", help="Parent POU exact tree path from plc_find")
@click.option("--dry-run", is_flag=True, help="Preview references without deleting")
@click.option("--force", is_flag=True, help="Delete even when references are found")
def plc_delete_member(pou: str, name: str, member_type: str, path: str,
                      dry_run: bool, force: bool):
    """Delete one member inside a POU or interface via COM."""
    from ._ps_bridge import com_delete_member
    try:
        r = com_delete_member(pou, name, member_type=member_type, path=path,
                              dry_run=dry_run, force=force)
        click.echo(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_plc.command("vars")
@click.argument("pou_name", required=False)
def plc_vars(pou_name: str | None):
    """List declared variables across the project (or one POU) via COM."""
    from ._ps_bridge import list_variables
    try:
        variables = list_variables(pou_name)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    if not variables:
        click.echo("  (no variables found)")
        return
    click.echo(f"\n  {len(variables)} variable(s)\n")
    cur = None
    for v in variables:
        if v["pou"] != cur:
            cur = v["pou"]
            click.echo(f"  [{cur}]")
        click.echo(f"    {v['name']:<28} : {v['type']:<24} ({v['scope']})")


@cmd_plc.command("structure")
def plc_structure():
    """Show the PLC project structure (folders → objects) via COM."""
    from ._ps_bridge import com_structure
    try:
        s = com_structure()
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    click.echo(f"\n  {s['project']}")
    if not s["folders"]:
        click.echo("    (empty)")
        return
    for folder, entries in s["folders"].items():
        click.echo(f"    {folder}/ ({len(entries)})")
        for e in entries:
            suffix = f"  [{', '.join(e['methods'])}]" if e["methods"] else ""
            click.echo(f"      {e['name']}{suffix}")


@cmd_plc.command("search")
@click.argument("pattern")
@click.option("--regex", is_flag=True, help="Treat pattern as a regular expression")
@click.option("--case", is_flag=True, help="Case-sensitive match")
def plc_search(pattern: str, regex: bool, case: bool):
    """Search all POU code (declaration + implementation) via COM."""
    from ._ps_bridge import search_code
    try:
        hits = search_code(pattern, ignore_case=not case, regex=regex)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    if not hits:
        click.echo("  (no matches)")
        return
    click.echo(f"\n  {len(hits)} match(es)\n")
    for h in hits:
        click.echo(f"  {h['pou']}:{h['area']}:{h['line']}: {h['text'][:100]}")


@cmd_plc.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--path", default="", help="Exact tree path from plc_find")
def plc_rename(old_name: str, new_name: str, path: str):
    """Rename a POU/DUT/GVL/Interface via COM."""
    from ._ps_bridge import com_rename
    try:
        r = com_rename(old_name, new_name, path=path)
        click.echo(f"  Renamed {r['old']} -> {r['new']} (in {r['folder']})")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("rename-member")
@click.argument("pou")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--path", default="", help="Parent POU exact tree path from plc_find")
def plc_rename_member(pou: str, old_name: str, new_name: str, path: str):
    """Rename one member inside a POU or interface."""
    from ._ps_bridge import com_rename_member
    try:
        r = com_rename_member(pou, old_name, new_name, path=path)
        click.echo(f"  Renamed {r['pou']}.{r['old']} -> {r['new']}")
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_plc.command("restore-snapshot")
@click.argument("snapshot", default="latest")
@click.option("--apply", "apply_restore", is_flag=True,
              help="Apply the restore; otherwise only preview")
def plc_restore_snapshot(snapshot: str, apply_restore: bool):
    """Preview or transactionally restore a PLC code snapshot."""
    from tc_agent.plc_versions import restore_snapshot
    try:
        click.echo(json.dumps(restore_snapshot(snapshot, apply=apply_restore),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_plc.command("import-com")
@click.argument("xml_file", type=click.Path(exists=True))
@click.option("--options", "-o", default=0, type=int,
              help="Import options: 0=Error, 1=Rename, 2=Replace, 3=Skip")
def plc_import_com(xml_file: str, options: int):
    """Import PLCopen XML via COM PlcOpenImport."""
    from ._ps_bridge import com_import_plcopen as import_plcopen
    try:
        r = import_plcopen(xml_file, options)
        click.echo(f"  Imported: {r['file']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("export-com")
@click.argument("output", type=click.Path())
@click.argument("pou_names", nargs=-1, required=True)
def plc_export_com(output: str, pou_names: tuple[str, ...]):
    """Export POUs via COM PlcOpenExport."""
    from ._ps_bridge import com_export_plcopen as export_plcopen
    try:
        r = export_plcopen(output, list(pou_names))
        click.echo(f"  Exported {len(r['pous'])} POUs to {r['file']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("libraries")
def plc_libraries():
    """List library references in the current project."""
    from ._ps_bridge import com_list_libraries as list_libraries
    try:
        libs = list_libraries()
        if not libs:
            click.echo("  (no libraries found or no project open)")
            return
        click.echo(f"\n  {len(libs)} libraries\n")
        for lib in libs:
            click.echo(f"  {lib['name']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("lib-scan")
def plc_lib_scan():
    """Scan all installed libraries on the system."""
    from ._ps_bridge import com_scan_libraries as scan_installed_libraries
    try:
        libs = scan_installed_libraries()
        click.echo(f"\n  {len(libs)} installed libraries\n")
        for lib in libs:
            click.echo(f"  {lib['name']:<40} v{lib['version']:<15} {lib['distributor']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("lib-add")
@click.argument("name")
@click.option("--version", "-v", default="*", help="Version (* for latest)")
@click.option("--distributor", "-d", default="", help="Distributor name")
def plc_lib_add(name: str, version: str, distributor: str):
    """Add a library reference to the project."""
    from ._ps_bridge import com_add_library
    try:
        r = com_add_library(name, version, distributor)
        click.echo(f"  Added: {r['library']} (v{r['version']})")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("lib-remove")
@click.argument("name")
@click.option("--version", "-v", default="", help="Version (empty = any)")
@click.option("--distributor", "-d", default="", help="Distributor (empty = any)")
def plc_lib_remove(name: str, version: str, distributor: str):
    """Remove a library reference from the project."""
    from ._ps_bridge import com_remove_library
    try:
        r = com_remove_library(name, version, distributor)
        click.echo(f"  Removed: {r['library']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("placeholder-add")
@click.argument("name")
@click.option("--lib", "-l", default="", help="Default library")
@click.option("--version", "-v", default="*", help="Default version")
@click.option("--distributor", "-d", default="", help="Default distributor")
def plc_placeholder_add(name: str, lib: str, version: str, distributor: str):
    """Add a placeholder reference to the project."""
    from ._ps_bridge import com_add_placeholder
    try:
        r = com_add_placeholder(name, lib, version, distributor)
        click.echo(f"  Added placeholder: {r['placeholder']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("placeholder-freeze")
@click.argument("name")
def plc_placeholder_freeze(name: str):
    """Freeze a placeholder's version (pin to current)."""
    from ._ps_bridge import com_freeze_placeholder
    try:
        r = com_freeze_placeholder(name)
        click.echo(f"  Frozen: {r['placeholder']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("repo-add")
@click.argument("name")
@click.argument("path")
@click.option("--index", "-i", default=0, help="Insert position")
def plc_repo_add(name: str, path: str, index: int):
    """Add a library repository."""
    from ._ps_bridge import com_insert_repository
    try:
        r = com_insert_repository(name, path, index)
        click.echo(f"  Repository added: {r['repository']} -> {r['path']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("repo-remove")
@click.argument("name")
def plc_repo_remove(name: str):
    """Remove a library repository."""
    from ._ps_bridge import com_remove_repository
    try:
        r = com_remove_repository(name)
        click.echo(f"  Repository removed: {r['repository']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("lib-install")
@click.argument("repo")
@click.argument("path")
@click.option("--overwrite", is_flag=True, help="Overwrite existing")
def plc_lib_install(repo: str, path: str, overwrite: bool):
    """Install a library from a repository."""
    from ._ps_bridge import com_install_library
    try:
        r = com_install_library(repo, path, overwrite)
        click.echo(f"  Installed: {r['path']} from {r['repository']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("lib-uninstall")
@click.argument("repo")
@click.argument("name")
@click.option("--version", "-v", default="", help="Version")
@click.option("--distributor", "-d", default="", help="Distributor")
def plc_lib_uninstall(repo: str, name: str, version: str, distributor: str):
    """Uninstall a library from a repository."""
    from ._ps_bridge import com_uninstall_library
    try:
        r = com_uninstall_library(repo, name, version, distributor)
        click.echo(f"  Uninstalled: {r['library']} from {repo}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_plc.command("build")
def plc_build():
    """Build PLC project and show errors (COM via PowerShell bridge)."""
    from ._ps_bridge import com_build
    try:
        r = com_build(always_read_errors=True)
    except Exception as e:
        raise click.ClickException(str(e)) from e

    failed = r.get("failedProjects", 0)
    click.echo(f"  Build: {'OK' if not failed else f'{failed} project(s) FAILED'}")
    errs = r.get("errors") or []
    if errs:
        click.echo(f"  Errors: {r.get('errorCount', len(errs))}")
        for e in errs:
            loc = f" ({e['file']}:{e['line']})" if e.get("file") else ""
            click.echo(f"    {e.get('description','')[:120]}{loc}")
    elif failed:
        # ErrorList 为空时回退到输出面板文本行
        for line in r.get("errorLines") or []:
            click.echo(f"    {line.strip()[:120]}")
        if not (r.get("errorLines") or []):
            click.echo(f"    [diagnostics] {r.get('message') or 'Compiler diagnostics were not returned.'}")
        if r.get("diagnosticsPending"):
            click.echo("    [diagnostics] Diagnostics are pending; retry the build to read the refreshed Error List.")
    for w in r.get("warnings") or []:
        click.echo(f"    [warn] {w.get('description','')[:110]}")
    error_count = int(r.get("errorCount", len(errs)) or 0)
    if failed or error_count:
        raise click.ClickException(
            f"PLC build failed ({failed} failed project(s), {error_count} error(s))"
        )


@cmd_plc.command("lint")
@click.option("--no-build", is_flag=True, help="Skip the plc build 0-error check")
def plc_lint(no_build: bool):
    """Lint the open PLC project against docs/plc_coding_standard.md.

    命名前缀 / FB 头注释 / 状态四件套 / 变量前缀 / 魔法错误码 / Tab 缩进，
    并（默认）跑一次编译确认 0 error。
    """
    from . import _ps_bridge as ps
    from .lint import lint_objects, summarize
    try:
        findings = lint_objects(ps.com_all_code())
    except Exception as e:
        raise click.ClickException(str(e)) from e
    s = summarize(findings)
    click.echo(f"\n  Lint: {s['total']} issue(s)")
    for rule, n in sorted(s["by_rule"].items()):
        click.echo(f"    {rule}: {n}")
    for f in findings:
        click.echo(f"    [{f['rule']}] {f['object']}: {f['message']}")
    build_errors = 0
    if not no_build:
        try:
            b = ps.com_build()
            build_errors = int(b.get("errorCount", 0) or 0)
            click.echo(f"  Build: {'OK (0 error)' if not build_errors else f'{build_errors} error(s)'}")
        except Exception as e:
            raise click.ClickException(f"PLC build check failed: {e}") from e
    if s["total"] or build_errors:
        raise click.ClickException(
            f"PLC quality gate failed ({s['total']} lint issue(s), "
            f"{build_errors} build error(s))"
        )


@cmd_plc.command("analyze")
@click.option("--max-complexity", default=20, show_default=True, type=click.IntRange(1, None),
              help="TCSA cognitive-complexity warning threshold")
@click.option("--rule", "rules", multiple=True, help="Per-run severity, e.g. SA0038=error (off/warning/error)")
def plc_analyze(max_complexity: int, rules: tuple[str, ...]):
    """Run offline TCSA static analysis without requiring TE1200.

    The analysis only reads source through the XAE COM bridge. It neither
    builds nor modifies the project. The result is deliberately not a TE1200
    result; it always states that TE1200 was not executed.
    """
    from . import _ps_bridge as ps
    from .static_analysis import analyze_objects
    try:
        policy = {}
        for setting in rules:
            key, separator, value = setting.partition('=')
            if not separator:
                raise ValueError('--rule requires SAxxxx=off/warning/error')
            policy[key.strip().upper()] = value.strip().lower()
        result = analyze_objects(ps.com_all_code(), max_complexity=max_complexity,
                                 rule_severities=policy)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    summary = result["summary"]
    click.echo(f"  TCSA: {summary['total']} finding(s) "
               f"({summary['errors']} error, {summary['warnings']} warning)")
    for finding in result["findings"]:
        location = f"{finding['area']}:{finding['line']}"
        click.echo(f"    [{finding['severity']}] {finding['rule']} "
                   f"{finding['object']} {location}: {finding['message']}")
    click.echo(f"  TE1200: {result['te1200_status']} — {result['disclaimer']}")
    if summary["errors"]:
        raise click.ClickException(
            f"TCSA found {summary['errors']} error-level finding(s); review before writing/deploying."
        )


@cmd_plc.command("reload")
def plc_reload():
    """No-op: COM writes are IDE-internal and re-parse automatically."""
    click.echo("  (COM writes auto-reparse — no reload needed)")


# ======================================================================
# hmi — TwinCAT HMI project/page tools
# ======================================================================

@main.group("hmi")
def cmd_hmi():
    """TwinCAT HMI project inspection, generation and build tools."""


@cmd_hmi.command("create-project")
@click.argument("name")
@click.option("--output", "output_directory", default="", type=click.Path(),
              help="Destination directory; defaults to <solution>/<name>.")
@click.option("--template", default="", type=click.Path(), help="Optional installed .vstemplate path.")
@click.option("--apply", is_flag=True, help="Create the project through XAE DTE.")
def hmi_create_project(name: str, output_directory: str, template: str, apply: bool):
    """Preview or create an HMI project from the installed TE2000 template."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_create_project(
            name, output_directory=output_directory, template=template, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("info")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_info(project: str):
    """Read version, startup view and project metadata from the open HMI project."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_project_info(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("structure")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_structure(project: str):
    """Inventory HMI views, contents, scripts, themes and server files."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_structure(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("read")
@click.argument("file")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--max-chars", default=200000, type=click.IntRange(1, 1000000), show_default=True)
@click.option("--control-id", default="", help="Return only the exact HMI control ID.")
@click.option("--include-content/--no-content", default=True, show_default=True,
              help="Include the raw file body; use --no-content for control inspection.")
@click.option("--max-controls", default=200, type=click.IntRange(1, 5000), show_default=True)
@click.option("--control-offset", default=0, type=click.IntRange(min=0), help="Continue from next_control_offset.")
@click.option("--content-offset", default=0, type=click.IntRange(min=0), help="Continue from next_content_offset (UTF-16 units).")
def hmi_read(file: str, project: str, max_chars: int, control_id: str,
             include_content: bool, max_controls: int, control_offset: int, content_offset: int):
    """Read one project-relative HMI file and parse controls/bindings."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_read(
            file, project, max_chars, control_id=control_id,
            include_content=include_content, max_controls=max_controls,
            control_offset=control_offset, content_offset=content_offset,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("source-index")
@click.option("--project", default="")
@click.option("--refresh", is_flag=True)
def hmi_source_index(project, refresh):
    """Incrementally index saved HMI files and controls; no XAE saves."""
    from .hmi_source import source_index
    try:
        click.echo(json.dumps(source_index(project, refresh), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("control-schema")
@click.option("--project", default="")
@click.option("--control-type", default="")
@click.option("--attribute", default="")
def hmi_control_schema(**kwargs):
    """Read installed control/attribute contracts before generating markup."""
    from .hmi_contract import control_schema
    try:
        click.echo(json.dumps(control_schema(**kwargs), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("source-catalog")
@click.option("--project", default="")
@click.option("--kind", type=click.Choice(['files', 'controls']), default='files')
@click.option("--file", default="")
@click.option("--query", default="")
@click.option("--offset", default=0, type=click.IntRange(min=0))
@click.option("--limit", default=80, type=click.IntRange(1, 100))
def hmi_source_catalog(**kwargs):
    """Page saved HMI files/controls using next_offset."""
    from .hmi_source import source_catalog
    try:
        click.echo(json.dumps(source_catalog(**kwargs), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("read-smart")
@click.argument("file")
@click.option("--project", default="")
@click.option("--area", type=click.Choice(['auto', 'controls', 'source', 'events', 'bindings']), default='auto')
@click.option("--control-id", default="")
@click.option("--include-content/--no-content", default=None)
@click.option("--max-controls", default=40, type=click.IntRange(1, 5000))
@click.option("--max-chars", default=12000, type=click.IntRange(1, 1000000))
@click.option("--control-offset", default=0, type=click.IntRange(min=0))
@click.option("--content-offset", default=0, type=click.IntRange(min=0))
@click.option("--refresh", is_flag=True)
def hmi_read_smart(**kwargs):
    """Read indexed saved HMI content; unsaved editor state remains unknown."""
    from .hmi_source import read_smart
    try:
        click.echo(json.dumps(read_smart(**kwargs), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("write-markup")
@click.argument("file")
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--apply", is_flag=True, help="Write through the XAE DTE text document and verify.")
def hmi_write_markup(file: str, source: str, project: str, apply: bool):
    """Preview or replace a markup document from a UTF-8 source file."""
    from . import _ps_bridge as ps
    try:
        markup = Path(source).read_text(encoding="utf-8")
        click.echo(json.dumps(ps.com_hmi_write_markup(file, markup, project=project, apply=apply),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("ads-info")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_ads_info(project: str):
    """Read default and remote ADS runtime definitions without connecting."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_ads_info(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("validate")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_validate(project: str):
    """Validate HMI markup, startup view and ADS configuration files."""
    from . import _ps_bridge as ps
    try:
        result = ps.com_hmi_validate(project)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("valid", False):
            raise click.ClickException(
                f"HMI validation found {result.get('error_count', 0)} error(s)"
            )
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("create-view")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--kind", type=click.Choice(["view", "content"]), default="view", show_default=True)
@click.option("--controls", default="[]", help="JSON array of {id,type,attributes}; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write the file and add it through DTE/COM.")
def hmi_create_view(name: str, project: str, kind: str, controls: str, apply: bool):
    """Preview or generate a View/Content and add it to the open HMI project."""
    from . import _ps_bridge as ps
    try:
        controls_path = Path(controls)
        payload = json.loads(controls_path.read_text(encoding="utf-8") if controls_path.is_file() else controls)
        if not isinstance(payload, list):
            raise ValueError("controls must be a JSON array")
        click.echo(json.dumps(
            ps.com_hmi_create_view(name, project=project, kind=kind,
                                   controls=payload, apply=apply),
            ensure_ascii=False, indent=2,
        ))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("project-api")
@click.argument("operation")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--arguments", default="{}", help="JSON object of operation arguments; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Call the mutating official ITcHmiProject method.")
def hmi_project_api(operation: str, project: str, arguments: str, apply: bool):
    """Inspect or call an official ITcHmiProject member; use catalog first."""
    from . import _ps_bridge as ps
    try:
        arguments_path = Path(arguments)
        payload = json.loads(arguments_path.read_text(encoding="utf-8") if arguments_path.is_file() else arguments)
        if not isinstance(payload, dict):
            raise ValueError("arguments must be a JSON object")
        click.echo(json.dumps(
            ps.com_hmi_project_api(operation, project=project, arguments=payload, apply=apply),
            ensure_ascii=False, indent=2,
        ))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("startup-view-set")
@click.argument("view")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_startup_view_set(view: str, project: str):
    """Set the HMI startup View through ITcHmiProject.ChangeStartupView."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(
            ps.com_hmi_startup_view_set(view, project=project),
            ensure_ascii=False, indent=2,
        ))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("control")
@click.argument("file")
@click.argument("action", type=click.Choice(["add", "update", "remove"]))
@click.argument("control_id")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--type", "control_type", default="", help="Full TcHmi.Controls.* type for add/update.")
@click.option("--parent", "parent_id", default="", help="Parent control ID when adding.")
@click.option("--attributes", default="{}", help="JSON object of data-tchmi-* attributes; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write through a DTE text document and verify without project reload.")
def hmi_control(file: str, action: str, control_id: str, project: str,
                control_type: str, parent_id: str, attributes: str, apply: bool):
    """Preview or add/update/remove one control in an HMI markup file."""
    from . import _ps_bridge as ps
    try:
        attributes_path = Path(attributes)
        payload = json.loads(attributes_path.read_text(encoding="utf-8") if attributes_path.is_file() else attributes)
        if not isinstance(payload, dict):
            raise ValueError("attributes must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_control_edit(
            file, action, control_id, project=project, control_type=control_type,
            parent_id=parent_id, attributes=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("delete-view")
@click.argument("file")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--apply", is_flag=True, help="Back up and delete the page, then reload the HMI project.")
def hmi_delete_view(file: str, project: str, apply: bool):
    """Preview or safely delete a non-startup View/Content."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_delete_view(file, project=project, apply=apply),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("ads-runtime-set")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--scope", type=click.Choice(["default", "remote", "both"]), default="both", show_default=True)
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON object with netid, port, enabled, read_only; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write ADS JSON and reload the HMI project.")
def hmi_ads_runtime_set(name: str, project: str, scope: str, action: str,
                        settings: str, apply: bool):
    """Preview or upsert/remove one saved HMI ADS Runtime."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_ads_runtime_set(
            name, project=project, scope=scope, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("ads-symbols")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--runtime", default="", help="Optional ADS Runtime filter.")
@click.option("--scope", type=click.Choice(["", "default", "remote"]), default="")
def hmi_ads_symbols(project: str, runtime: str, scope: str):
    """Read saved ADS symbol mappings without connecting to the PLC."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_ads_symbols(project=project, runtime=runtime, scope=scope),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("ads-symbol-set")
@click.argument("runtime")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--scope", type=click.Choice(["default", "remote", "both"]), default="both", show_default=True)
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--index-group", type=click.IntRange(0, 4294967295), default=0, show_default=True)
@click.option("--index-offset", type=click.IntRange(0, 4294967295), default=0, show_default=True)
@click.option("--type-name", default="", help="Required for upsert, e.g. BOOL or DINT.")
@click.option("--apply", is_flag=True, help="Write ADS JSON, reload the project and verify readback.")
def hmi_ads_symbol_set(runtime: str, name: str, project: str, scope: str, action: str,
                       index_group: int, index_offset: int, type_name: str, apply: bool):
    """Preview or upsert/remove one schema-backed ADS mapped symbol."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_ads_symbol_set(
            runtime, name, project=project, scope=scope, action=action,
            index_group=index_group, index_offset=index_offset, type_name=type_name, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("dynamic-symbols-set")
@click.argument("symbols_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--definitions", "definitions_file", default="", type=click.Path(exists=True, dir_okay=False),
              help="Optional JSON object containing ADS schema definitions.")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--apply", is_flag=True, help="Unload, update TcHmiSrv config, reload and verify.")
def hmi_dynamic_symbols_set(symbols_file: str, definitions_file: str, project: str, apply: bool):
    """Preview or register schema-backed dynamic ADS server symbols."""
    from . import _ps_bridge as ps
    try:
        symbols = json.loads(Path(symbols_file).read_text(encoding="utf-8"))
        definitions = (json.loads(Path(definitions_file).read_text(encoding="utf-8"))
                       if definitions_file else {})
        if not isinstance(symbols, dict) or not isinstance(definitions, dict):
            raise ValueError("symbols and definitions must be JSON objects")
        click.echo(json.dumps(ps.com_hmi_dynamic_symbols_set(
            symbols, definitions=definitions, project=project, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("bind-plc")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--plc", default="", help="PLC project/runtime name; required when multiple PLCs are open.")
@click.option("--runtime", "runtime_name", default="", help="HMI ADS Runtime name; defaults to the existing sole Runtime or PLC1.")
@click.option("--symbol-root", "symbol_roots", multiple=True,
              help="PLC symbol root to expose; repeatable, defaults to GVL_Hmi.")
@click.option("--read-only-symbol", "read_only_symbols", multiple=True,
              help="Selected PLC symbol/root to expose read-only; repeatable.")
@click.option("--scope", type=click.Choice(["default", "both"]), default="default", show_default=True)
@click.option("--apply", is_flag=True, help="Atomically update ADS/TcHmiSrv configs, reload and verify.")
def hmi_bind_plc(project: str, plc: str, runtime_name: str, symbol_roots: tuple[str, ...],
                 read_only_symbols: tuple[str, ...], scope: str, apply: bool):
    """Bind selected TMC symbols to HMI using the actual XAE target and PLC ADS port."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_bind_plc(
            project=project, plc=plc, runtime_name=runtime_name,
            symbol_roots=list(symbol_roots), read_only_symbols=list(read_only_symbols),
            scope=scope, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("bindings")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_bindings(project: str):
    """Audit HMI SymbolExpressions and their saved references."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_bindings(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("internal-symbols")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_internal_symbols(project: str):
    """Read project internal symbols from tchmiconfig.json."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_internal_symbols(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("internal-symbol-set")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON object with type, value, persist, readonly; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write tchmiconfig.json, reload the project and verify readback.")
def hmi_internal_symbol_set(name: str, project: str, action: str, settings: str, apply: bool):
    """Preview or upsert/remove one Framework internal symbol."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_internal_symbol_set(
            name, project=project, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("localizations")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_localizations(project: str):
    """Read project languages, localization keys and missing values."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_localizations(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("localization-set")
@click.argument("key")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--values", default="{}", help="JSON locale/text map; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write localization files, reload and verify readback.")
def hmi_localization_set(key: str, project: str, action: str, values: str, apply: bool):
    """Preview or upsert/remove one localization key."""
    from . import _ps_bridge as ps
    try:
        values_path = Path(values)
        payload = json.loads(values_path.read_text(encoding="utf-8") if values_path.is_file() else values)
        if not isinstance(payload, dict):
            raise ValueError("values must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_localization_set(
            key, project=project, action=action, values=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("themes")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_themes(project: str):
    """Read themes, active theme and project themed resources."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_themes(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("themed-resource-set")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON with type, description and per-theme values; or file path.")
@click.option("--apply", is_flag=True, help="Write tchmiconfig.json, reload and verify readback.")
def hmi_themed_resource_set(name: str, project: str, action: str, settings: str, apply: bool):
    """Preview or upsert/remove one project themed resource."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_themed_resource_set(
            name, project=project, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("active-theme-set")
@click.argument("theme")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--apply", is_flag=True, help="Write activeTheme, reload and verify readback.")
def hmi_active_theme_set(theme: str, project: str, apply: bool):
    """Preview or set the startup active theme."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_active_theme_set(
            theme, project=project, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("user-controls")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_user_controls(project: str):
    """Read registered UserControls, parameters and contained controls."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_user_controls(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("user-control-create")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--parameters", default="[]", help="JSON parameter array; or a JSON file path.")
@click.option("--controls", default="[]", help="JSON control array; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Create both files, register, reload and verify readback.")
def hmi_user_control_create(name: str, project: str, parameters: str, controls: str, apply: bool):
    """Preview or create a schema-backed 1.12 UserControl."""
    from . import _ps_bridge as ps
    try:
        parameters_path = Path(parameters)
        parameter_payload = json.loads(parameters_path.read_text(encoding="utf-8") if parameters_path.is_file() else parameters)
        controls_path = Path(controls)
        control_payload = json.loads(controls_path.read_text(encoding="utf-8") if controls_path.is_file() else controls)
        if not isinstance(parameter_payload, list) or not isinstance(control_payload, list):
            raise ValueError("parameters and controls must be JSON arrays")
        click.echo(json.dumps(ps.com_hmi_user_control_create(
            name, project=project, parameters=parameter_payload, controls=control_payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("user-control-parameter-set")
@click.argument("user_control")
@click.argument("name")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON UserControl parameter object; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Write parameter config, reload and verify readback.")
def hmi_user_control_parameter_set(user_control: str, name: str, project: str,
                                   action: str, settings: str, apply: bool):
    """Preview or upsert/remove one UserControl parameter."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_user_control_parameter_set(
            user_control, name, project=project, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("user-control-delete")
@click.argument("user_control")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
@click.option("--apply", is_flag=True, help="Back up, delete registrations/files, reload and verify.")
def hmi_user_control_delete(user_control: str, project: str, apply: bool):
    """Preview or back up and delete one UserControl."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_user_control_delete(
            user_control, project=project, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-templates")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_framework_templates(project: str):
    """List installed TE2000 Framework Project templates."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_templates(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-validate")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
def hmi_framework_validate(source: Path):
    """Validate a Framework Project and its control resources."""
    from . import _ps_bridge as ps
    try:
        result = ps.com_hmi_framework_validate(str(source))
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("valid", False):
            raise click.ClickException("Framework project validation failed")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-control-info")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--control", default="", help="Control name or basePath when the project contains multiple controls.")
def hmi_framework_control_info(source: Path, control: str):
    """Read one Framework Control attribute/event contract."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_control_info(
            str(source), control=control,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-attribute-set")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.argument("name")
@click.option("--control", default="", help="Control name or basePath when the project contains multiple controls.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON attribute settings object; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Back up and update Description plus generated accessor code.")
def hmi_framework_attribute_set(source: Path, name: str, control: str,
                                action: str, settings: str, apply: bool):
    """Preview or synchronize one Framework Control attribute."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_framework_attribute_set(
            str(source), name, control=control, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-event-set")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.argument("name")
@click.option("--control", default="", help="Control name or basePath when the project contains multiple controls.")
@click.option("--action", type=click.Choice(["upsert", "remove"]), default="upsert", show_default=True)
@click.option("--settings", default="{}", help="JSON event settings object; or a JSON file path.")
@click.option("--apply", is_flag=True, help="Back up and update Description plus generated raise helper.")
def hmi_framework_event_set(source: Path, name: str, control: str,
                            action: str, settings: str, apply: bool):
    """Preview or synchronize one Framework Control event."""
    from . import _ps_bridge as ps
    try:
        settings_path = Path(settings)
        payload = json.loads(settings_path.read_text(encoding="utf-8") if settings_path.is_file() else settings)
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        click.echo(json.dumps(ps.com_hmi_framework_event_set(
            str(source), name, control=control, action=action, settings=payload, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-create")
@click.argument("name")
@click.option("--output", "output_directory", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Existing parent directory for the new Framework project.")
@click.option("--project", default="", help="HMI project used to select the Framework target.")
@click.option("--language", type=click.Choice(["typescript", "javascript"]),
              default="typescript", show_default=True)
@click.option("--description", default="", help="NuGet package description.")
@click.option("--apply", is_flag=True, help="Create files from the installed TE2000 template and validate them.")
def hmi_framework_create(name: str, output_directory: Path, project: str,
                         language: str, description: str, apply: bool):
    """Preview or create a native1.12 Framework Control project."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_create(
            name, str(output_directory), project=project, language=language,
            description=description, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-pack")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--output", "output_directory", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              help="Package output directory; defaults to project bin/Packages.")
@click.option("--version", default="", help="Optional package version override.")
@click.option("--apply", is_flag=True, help="Create and inspect the local .nupkg; does not install or publish.")
def hmi_framework_pack(source: Path, output_directory: Path,
                       version: str, apply: bool):
    """Preview or pack a Framework Control NuGet package."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_pack(
            str(source), output_directory=str(output_directory or ""),
            version=version, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-packages")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--package-id", default="", help="Exact installed package ID; do not guess versions or paths.")
def hmi_framework_packages(project: str, package_id: str):
    """Inventory installed HMI packages and registration consistency."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_packages(project, package_id), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-package-inspect")
@click.argument("package", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def hmi_framework_package_inspect(package: Path):
    """Inspect a local HMI control/function/framework/resource archive without installing."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_package_inspect(str(package)), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-install")
@click.argument("package", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--apply", is_flag=True, help="Install, register and reload the HMI project.")
@click.option("--acknowledge-package-change", is_flag=True,
              help="Required with --apply: confirm modification of HMI package dependencies.")
def hmi_framework_install(package: Path, project: str, apply: bool,
                          acknowledge_package_change: bool):
    """Preview or transactionally install a Framework Control package."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_install(
            str(package), project=project, apply=apply,
            acknowledge_package_change=acknowledge_package_change,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("framework-uninstall")
@click.argument("package_id")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--force", is_flag=True, help="Allow removal after reviewing remaining markup references.")
@click.option("--apply", is_flag=True, help="Unregister, back up and remove the package from the project.")
@click.option("--acknowledge-package-change", is_flag=True,
              help="Required with --apply: confirm modification of HMI package dependencies.")
def hmi_framework_uninstall(package_id: str, project: str, force: bool, apply: bool,
                            acknowledge_package_change: bool):
    """Preview or transactionally uninstall a non-core Framework package."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_framework_uninstall(
            package_id, project=project, force=force, apply=apply,
            acknowledge_package_change=acknowledge_package_change,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("runtime-info")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
def hmi_runtime_info(project: str):
    """Discover the running HMI Engineering Server and app URL."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_runtime_info(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("server-control")
@click.argument("action", type=click.Choice(["start", "stop", "restart"]))
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--apply", is_flag=True, help="Perform the lifecycle action and verify the result.")
def hmi_server_control(action: str, project: str, apply: bool):
    """Preview or control the exact project's hidden Engineering Server process."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_server_control(
            action, project=project, apply=apply,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("ads-live-check")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--runtime", default="", help="Enabled default HMI ADS Runtime name.")
@click.option("--plc", default="", help="PLC runtime name; required when endpoint matching is ambiguous.")
@click.option("--symbol", "symbols", multiple=True,
              help="Configured HMI server symbol or mapped PLC symbol; repeatable. Defaults to all dynamic symbols.")
@click.option("--max-depth", default=3, show_default=True, type=click.IntRange(0, 8))
@click.option("--max-symbols", default=32, show_default=True, type=click.IntRange(1, 32))
def hmi_ads_live_check(project: str, runtime: str, plc: str, symbols: tuple[str, ...],
                       max_depth: int, max_symbols: int):
    """Read mapped PLC symbols through ADS without writing values or starting PLC/HMI."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_ads_live_check(
            project=project, runtime=runtime, plc=plc, symbols=list(symbols),
            max_depth=max_depth, max_symbols=max_symbols,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("binding-diagnose")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--runtime", default="", help="Enabled default HMI ADS Runtime name.")
@click.option("--plc", default="", help="PLC runtime name when more than one PLC is open.")
@click.option("--max-symbols", default=32, show_default=True, type=click.IntRange(1, 32))
def hmi_binding_diagnose(project: str, runtime: str, plc: str, max_symbols: int):
    """Diagnose the complete saved HMI-to-PLC binding chain without changing it."""
    from . import _ps_bridge as ps
    try:
        click.echo(json.dumps(ps.com_hmi_binding_diagnose(
            project=project, runtime=runtime, plc=plc, max_symbols=max_symbols,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("browser-validate")
@click.option("--project", default="", help="HMI project name or .hmiproj path.")
@click.option("--width", "widths", multiple=True, type=click.IntRange(320, 3840),
              help="Viewport width; repeat up to four times. Default: 1280.")
@click.option("--height", default=720, show_default=True, type=click.IntRange(240, 2160))
@click.option("--settle-ms", default=5000, show_default=True, type=click.IntRange(500, 30000))
@click.option("--entry-page", default="", help="Explicit saved .view to load, for example Main.view.")
def hmi_browser_validate(project: str, widths: tuple[int, ...], height: int, settle_ms: int,
                         entry_page: str):
    """Validate the running HMI in a temporary hidden browser."""
    from . import _ps_bridge as ps
    try:
        result = ps.com_hmi_browser_validate(
            project=project, widths=list(widths) or [1280], height=height,
            settle_ms=settle_ms, entry_page=entry_page,
        )
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success", False):
            raise click.ClickException("HMI browser runtime validation failed")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hmi.command("build")
@click.option("--project", default="", help="HMI project name; required when multiple are open.")
def hmi_build(project: str):
    """Build one HMI project without focusing or copying the Error List."""
    from . import _ps_bridge as ps
    try:
        result = ps.com_hmi_build(project)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success", False):
            raise click.ClickException("HMI build failed; diagnostics_pending=true")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


# ======================================================================
# tc — TwinCAT platform control (build, deploy, mode switch)
# ======================================================================

@main.group("tc")
def cmd_tc():
    """Platform control: build, activate, login, run/config mode."""


@cmd_tc.command("build")
def tc_build():
    """Build the PLC project and show errors."""
    r = build_plc_project()
    click.echo(f"  Build done. Errors: {r.get('error_count', 0)}")
    for e in r.get("errors", []):
        click.echo(f"    [{e['severity']}] {e['description'][:120]}")
    error_count = int(r.get("error_count", 0) or 0)
    if error_count:
        raise click.ClickException(f"TwinCAT build failed with {error_count} error(s)")


@cmd_tc.command("check")
def tc_check():
    """Check all PLC objects (compile check)."""
    r = check_all_objects()
    click.echo(f"  Check done. Errors: {r.get('error_count', 0)}")
    error_count = int(r.get("error_count", 0) or 0)
    if error_count:
        raise click.ClickException(f"PLC check failed with {error_count} error(s)")


@cmd_tc.command("activate")
@click.option('--restart', is_flag=True, help='Restart explicitly after submitting configuration.')
def tc_activate(restart):
    """Submit configuration; restart only with --restart."""
    from . import _ps_bridge as ps
    try:
        result = ps.com_activate()
        if restart:
            result = ps.com_restart()
        click.echo(json.dumps(result, ensure_ascii=False, default=str))
        if restart and not result.get('verified'):
            raise click.ClickException('Restart was not verified')
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("restart")
def tc_restart():
    """Restart the TwinCAT runtime (standalone, no re-activate)."""
    from . import _ps_bridge as ps
    try:
        r = ps.com_restart()
        click.echo(f"  TwinCAT {r.get('status', 'restarted')}")
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("projects")
def tc_projects():
    """List PLC projects (TIPC) and solution projects."""
    try:
        r = list_projects()
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    click.echo("\n  PLC projects:")
    for p in r["plc_projects"] or ["(none)"]:
        click.echo(f"    {p}")
    click.echo("  Solution projects:")
    for p in r["solution_projects"] or ["(none)"]:
        click.echo(f"    {p}")


@cmd_tc.command("project-info")
def tc_project_info():
    """Show solution/target metadata (solution, target NetId, PLC projects)."""
    try:
        r = get_project_info()
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    click.echo(f"\n  Solution:    {r.get('solution', '?')}")
    click.echo(f"  Projects:    {r.get('project_count', '?')}")
    click.echo(f"  Target NetId:{r.get('target_netid', '?')}")
    click.echo(f"  PLC projects:{', '.join(r.get('plc_projects', [])) or '(none)'}")


@cmd_tc.command("system-structure")
@click.option("--depth", default=6, type=click.IntRange(0, 12), show_default=True)
@click.option("--root", "roots", multiple=True, type=click.Choice(["TIRC", "TIRS", "TIRT"]))
def tc_system_structure(depth: int, roots: tuple[str, ...]):
    """Read the live SYSTEM tree (TIRC/TIRS/TIRT)."""
    try:
        click.echo(json.dumps(com_system_structure(depth, list(roots) or None), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("system-settings")
def tc_system_settings_cmd():
    """Read SYSTEM > Real-Time > Settings, CPU and task attributes."""
    try:
        click.echo(json.dumps(com_system_settings(), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("system-settings-set")
@click.argument("settings_json")
@click.option("--apply", is_flag=True, help="Write TIRS and verify readback; without it only previews.")
def tc_system_settings_set_cmd(settings_json: str, apply: bool):
    """Preview or write allow-listed TIRS settings from a JSON object."""
    try:
        settings = json.loads(settings_json)
        if not isinstance(settings, dict):
            raise ValueError("SETTINGS_JSON must be a JSON object")
        click.echo(json.dumps(com_system_settings_set(settings, apply=apply),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("core-info")
def tc_core_info_cmd():
    """Read TwinCAT CPU/core and affinity allocation."""
    try:
        click.echo(json.dumps(com_core_info(), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("realtime-info")
def tc_realtime_info_cmd():
    """Read normalized version, memory, core and task real-time settings."""
    try:
        click.echo(json.dumps(com_realtime_info(), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("realtime-validate")
def tc_realtime_validate_cmd():
    """Validate RT memory, cores, priorities and task timing."""
    try:
        click.echo(json.dumps(com_realtime_validate(), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-structure")
@click.option("--depth", default=8, type=click.IntRange(0, 12), show_default=True)
def tc_safety_structure_cmd(depth: int):
    """Read the TwinSAFE TISC tree without changing it."""
    try:
        click.echo(json.dumps(com_safety_structure(depth), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-info")
@click.argument("project", required=False, default="")
def tc_safety_info_cmd(project: str):
    """Read Safety project metadata visible through Automation Interface."""
    try:
        click.echo(json.dumps(com_safety_project_info(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-files")
@click.argument("project")
def tc_safety_files_cmd(project: str):
    """Inventory files in a TISC or filesystem Safety project."""
    try:
        click.echo(json.dumps(com_safety_files(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-target-info")
@click.argument("project")
def tc_safety_target_info_cmd(project: str):
    """Read Safety target configuration without hardware validation."""
    try:
        click.echo(json.dumps(com_safety_target_info(project), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-aliases")
@click.argument("project")
@click.option("--group", default="")
def tc_safety_aliases_cmd(project: str, group: str):
    """Read Safety Alias Devices, channels and stored mappings."""
    try:
        click.echo(json.dumps(com_safety_aliases(project, group=group), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-application")
@click.argument("project")
@click.option("--group", default="")
def tc_safety_application_cmd(project: str, group: str):
    """Read graphical SAL or Safety C application structure."""
    try:
        click.echo(json.dumps(com_safety_application(project, group=group), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-logic-check")
@click.argument("project")
@click.option("--group", default="")
def tc_safety_logic_check_cmd(project: str, group: str):
    """Check stored TwinSAFE FB/port/wire and Alias Device references."""
    try:
        click.echo(json.dumps(com_safety_logic_check(project, group=group), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-validate")
@click.argument("source", required=False, default="")
def tc_safety_validate_cmd(source: str):
    """Structurally validate TISC and optionally a Safety template."""
    try:
        click.echo(json.dumps(com_safety_validate(source), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-import")
@click.argument("source")
@click.option("--name", default="", help="Project name for copy/move mode.")
@click.option("--mode", type=click.Choice(["copy", "move", "reference"]), default="copy", show_default=True)
@click.option("--apply", is_flag=True, help="Import; without it only previews.")
@click.option("--confirm-source-move", is_flag=True)
@click.option("--acknowledge-safety-review", is_flag=True)
def tc_safety_import_cmd(source: str, name: str, mode: str, apply: bool,
                         confirm_source_move: bool, acknowledge_safety_review: bool):
    """Preview/import a .splcproj or .tfzip into TISC."""
    try:
        click.echo(json.dumps(com_safety_import(
            source, name=name, mode=mode, apply=apply,
            confirm_source_move=confirm_source_move,
            acknowledge_safety_review=acknowledge_safety_review,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-create")
@click.argument("name")
@click.option("--target", type=click.Choice(["hardware", "twincat-safety-plc"]),
              default="hardware", show_default=True)
@click.option("--template", "template_name",
              type=click.Choice(["empty", "preconfigured-errack", "preconfigured-inputs"]),
              default="preconfigured-inputs", show_default=True)
@click.option("--author", default="TwinCAT Agent", show_default=True)
@click.option("--internal-project-name", default="")
@click.option("--apply", is_flag=True, help="Create; without it only previews.")
@click.option("--acknowledge-safety-review", is_flag=True)
def tc_safety_create_cmd(name: str, target: str, template_name: str, author: str,
                         internal_project_name: str, apply: bool,
                         acknowledge_safety_review: bool):
    """Create a project from an installed Beckhoff Safety template."""
    try:
        click.echo(json.dumps(com_safety_create(
            name, target=target, template=template_name, author=author,
            internal_project_name=internal_project_name, apply=apply,
            acknowledge_safety_review=acknowledge_safety_review,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-export")
@click.argument("project")
@click.argument("output_file")
@click.option("--overwrite", is_flag=True)
@click.option("--apply", is_flag=True, help="Export; without it only previews.")
@click.option("--acknowledge-safety-review", is_flag=True)
def tc_safety_export_cmd(project: str, output_file: str, overwrite: bool,
                         apply: bool, acknowledge_safety_review: bool):
    """Preview/export one Safety project to .tfzip."""
    try:
        click.echo(json.dumps(com_safety_export(
            project, output_file, overwrite=overwrite, apply=apply,
            acknowledge_safety_review=acknowledge_safety_review,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-remove")
@click.argument("project")
@click.option("--apply", is_flag=True, help="Remove; without it only previews.")
@click.option("--confirm-project-name", default="")
@click.option("--acknowledge-safety-review", is_flag=True)
def tc_safety_remove_cmd(project: str, apply: bool, confirm_project_name: str,
                         acknowledge_safety_review: bool):
    """Explicitly remove from TISC while preserving files; normal deletion uses safety-delete."""
    try:
        click.echo(json.dumps(com_safety_remove(
            project, apply=apply, confirm_project_name=confirm_project_name,
            acknowledge_safety_review=acknowledge_safety_review,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("safety-delete")
@click.argument("project")
@click.option("--backup-file", default="")
@click.option("--apply", is_flag=True, help="Back up and delete; without it only previews.")
@click.option("--confirm-project-name", default="")
@click.option("--confirm-delete-files", is_flag=True)
@click.option("--acknowledge-safety-review", is_flag=True)
def tc_safety_delete_cmd(project: str, backup_file: str, apply: bool,
                         confirm_project_name: str, confirm_delete_files: bool,
                         acknowledge_safety_review: bool):
    """Back up and delete TISC plus files; also accepts an orphan project path."""
    try:
        click.echo(json.dumps(com_safety_delete(
            project, backup_file=backup_file, apply=apply,
            confirm_project_name=confirm_project_name,
            confirm_delete_files=confirm_delete_files,
            acknowledge_safety_review=acknowledge_safety_review,
        ), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("realtime-settings-set")
@click.argument("settings_json")
@click.option("--apply", is_flag=True, help="Write TIRS and verify; without it only previews.")
def tc_realtime_settings_set_cmd(settings_json: str, apply: bool):
    """Preview/write memory and per-core Real-Time settings."""
    try:
        settings = json.loads(settings_json)
        if not isinstance(settings, dict):
            raise ValueError("SETTINGS_JSON must be a JSON object")
        click.echo(json.dumps(com_system_settings_set(settings, apply=apply),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("core-assign")
@click.option("--cpu-id", "cpu_ids", multiple=True, type=click.IntRange(0, 4095), required=True)
@click.option("--max-cpus", type=click.IntRange(1, 4095))
@click.option("--affinity", type=click.IntRange(0, None), help="整体实时核心位掩码；省略时按 --cpu-id 自动生成。")
@click.option("--p-core-affinity", type=click.IntRange(0, None))
@click.option("--e-core-affinity", type=click.IntRange(0, None))
@click.option("--apply", is_flag=True, help="Write TIRS and verify readback; without it only previews.")
def tc_core_assign_cmd(cpu_ids: tuple[int, ...], max_cpus: int | None,
                       affinity: int | None,
                       p_core_affinity: int | None, e_core_affinity: int | None,
                       apply: bool):
    """Preview or assign TwinCAT CPU cores."""
    try:
        result = com_core_assign(list(cpu_ids), max_cpus=max_cpus,
                                 affinity=affinity,
                                 p_core_affinity=p_core_affinity,
                                 e_core_affinity=e_core_affinity, apply=apply)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("task-info")
@click.option("--depth", default=6, type=click.IntRange(0, 12), show_default=True)
def tc_task_info_cmd(depth: int):
    """Read the live TIRT task tree and parameters."""
    try:
        click.echo(json.dumps(com_task_info(max_depth=depth), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("task-core-assign")
@click.argument("task_path")
@click.argument("cpu_id", type=click.IntRange(0, 4095))
@click.option("--apply", is_flag=True, help="Write and verify; without it only previews.")
def tc_task_core_assign_cmd(task_path: str, cpu_id: int, apply: bool):
    """Preview or assign one TIRT task to a CPU core."""
    try:
        click.echo(json.dumps(com_task_core_assign(task_path, cpu_id, apply=apply), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("task-settings-set")
@click.argument("task_path")
@click.argument("settings_json")
@click.option("--apply", is_flag=True, help="Write and verify; without it only previews.")
def tc_task_settings_set_cmd(task_path: str, settings_json: str, apply: bool):
    """Preview/write one task's priority, cycle and watchdog settings."""
    try:
        settings = json.loads(settings_json)
        if not isinstance(settings, dict):
            raise ValueError("SETTINGS_JSON must be a JSON object")
        click.echo(json.dumps(com_task_settings_set(task_path, settings, apply=apply),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("system-add")
@click.argument("parent_path")
@click.argument("name")
@click.argument("item_type", type=click.IntRange(0, 65535))
@click.option("--info", default="", help="CreateChild vInfo.")
@click.option("--apply", is_flag=True, help="Create and verify; without it only previews.")
def tc_system_add_cmd(parent_path: str, name: str, item_type: int, info: str, apply: bool):
    """Preview or add a SYSTEM child under TIRC/TIRT."""
    try:
        click.echo(json.dumps(com_system_add(parent_path, name, item_type, info=info, apply=apply), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("system-remove")
@click.argument("path")
@click.option("--allow-with-children", is_flag=True)
@click.option("--apply", is_flag=True, help="Delete and verify; without it only previews.")
def tc_system_remove_cmd(path: str, allow_with_children: bool, apply: bool):
    """Preview or remove one exact SYSTEM child under TIRC/TIRT."""
    try:
        click.echo(json.dumps(com_system_remove(path, apply=apply,
                                                allow_with_children=allow_with_children),
                              ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("realtime-refresh")
def tc_realtime_refresh_cmd():
    """Refresh the currently open XAE SYSTEM > Real-Time designer only."""
    try:
        click.echo(json.dumps(com_realtime_refresh(), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_tc.command("io-structure")
@click.option("--depth", default=6, type=int, help="Max tree depth to traverse")
def tc_io_structure(depth: int):
    """Show the I/O (TIID) device tree via COM."""
    try:
        tree = get_io_structure(max_depth=depth)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return

    def render(node, indent):
        click.echo(f"{'  ' * indent}{node['name']}")
        for c in node["children"]:
            render(c, indent + 1)

    click.echo("")
    render(tree, 1)


@cmd_tc.command("device-read")
@click.argument("tree_path")
def tc_device_read(tree_path: str):
    """Read a tree item's settings XML via ProduceXml (e.g. TIID^Device 1 ...)."""
    try:
        r = read_device_settings(tree_path)
        click.echo(r["xml"])
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_tc.command("device-write")
@click.argument("tree_path")
@click.argument("xml_file", type=click.Path(exists=True))
def tc_device_write(tree_path: str, xml_file: str):
    """Write a tree item's settings via ConsumeXml (payload read from XML_FILE)."""
    try:
        with open(xml_file, "r", encoding="utf-8") as fh:
            xml = fh.read()
        r = write_device_settings(tree_path, xml)
        click.echo(f"  {r['status']}: {r['path']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_tc.command("ethercat-adapter")
def tc_ethercat_adapter():
    """Show each I/O device's bound network adapter (NIC)."""
    try:
        adapters = get_ethercat_master_adapter()
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    if not adapters:
        click.echo("  (no network-bound I/O devices found)")
        return
    click.echo("")
    for a in adapters:
        click.echo(f"  {a['device']}")
        click.echo(f"    desc: {a['adapter_desc'] or '?'}")
        click.echo(f"    name: {a['adapter_name'] or '?'}")
        click.echo(f"    mac : {a['mac'] or '?'}")


@cmd_tc.command("ethercat-adapter-set")
@click.argument("device_name")
@click.argument("adapter_desc")
def tc_ethercat_adapter_set(device_name: str, adapter_desc: str):
    """Rebind an I/O device to a different NIC by DeviceDesc (best-effort)."""
    try:
        r = change_ethercat_master_adapter(device_name, adapter_desc)
        click.echo(f"  {r['status']}: {r['device']} -> {r['adapter_desc']}")
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_tc.command("login")
@click.option('--runtime', default='')
@click.option('--all-plcs', is_flag=True)
def tc_login(runtime, all_plcs):
    """Login to PLC runtime."""
    from ._ps_bridge import com_login
    r = com_login(runtime, all_plcs)
    _print_runtime_transition(r)


@cmd_tc.command("logout")
@click.option('--runtime', default='')
@click.option('--all-plcs', is_flag=True)
def tc_logout(runtime, all_plcs):
    """Logout from PLC runtime."""
    from ._ps_bridge import com_logout
    r = com_logout(runtime, all_plcs)
    _print_runtime_transition(r)


@cmd_tc.command("start")
@click.option('--runtime', default='')
@click.option('--all-plcs', is_flag=True)
def tc_start(runtime, all_plcs):
    """Start PLC program."""
    from ._ps_bridge import com_start
    r = com_start(runtime, all_plcs)
    _print_runtime_transition(r)


@cmd_tc.command("stop")
@click.option('--runtime', default='')
@click.option('--all-plcs', is_flag=True)
def tc_stop(runtime, all_plcs):
    """Stop PLC program."""
    from ._ps_bridge import com_stop
    r = com_stop(runtime, all_plcs)
    _print_runtime_transition(r)


@cmd_tc.command("online")
@click.option('--runtime', default='')
@click.option('--all-plcs', is_flag=True)
def tc_online(runtime, all_plcs):
    """Login + Start (full online cycle)."""
    from ._ps_bridge import com_online
    r = com_online(runtime, all_plcs)
    _print_runtime_transition(r)


def _print_runtime_transition(result):
    click.echo(json.dumps(result, ensure_ascii=False, default=str))
    if result.get('verified') is not True:
        raise click.ClickException('Runtime transition not verified; see per-PLC evidence above')


@cmd_tc.command("state")
def tc_state():
    """Show current TwinCAT runtime state."""
    from ._ps_bridge import com_state
    r = com_state()
    click.echo(f"  State: {r.get('state', '?')}")
    click.echo(f"  Is Started: {r.get('started', '?')}")


@cmd_tc.command("config")
def tc_config():
    """Switch to Config mode."""
    from ._ps_bridge import com_config_mode
    r = com_config_mode()
    click.echo(f"  Mode: {r.get('mode', 'Config')}")


@cmd_tc.command("run")
def tc_run():
    """Switch to Run mode."""
    from ._ps_bridge import com_run_mode
    r = com_run_mode()
    click.echo(f"  Mode: {r.get('mode', 'Run')}")


# ------------------------------------------------------------------
# target — route management  (subgroup)
# ------------------------------------------------------------------

@cmd_tc.group("target", invoke_without_command=True)
@click.pass_context
def tc_target(ctx):
    """Target management: show, list, or set AMS NetId routes."""
    if ctx.invoked_subcommand is None:
        # Backward-compatible: default to 'show' when no subcommand given
        netid = get_target_net_id()
        click.echo(f"  Target NetId: {netid}")


@tc_target.command("show")
def tc_target_show():
    """Show the currently configured target NetId."""
    netid = get_target_net_id()
    click.echo(f"  Target NetId: {netid}")


@tc_target.command("list")
def tc_target_list():
    """List all available targets from StaticRoutes.xml."""
    routes = list_static_routes()
    if not routes:
        click.echo("  No static routes found in StaticRoutes.xml.")
        click.echo("  Add routes via TwinCAT XAE toolbar → Route Settings.")
        return

    click.echo(f"\n  {len(routes)} route(s) from StaticRoutes.xml\n")
    # Header
    click.echo(f"  {'Name':<26} {'NetId':<22} {'Address':<18} {'Type':<8} {'Flags'}")
    click.echo(f"  {'-'*26} {'-'*22} {'-'*18} {'-'*8} {'-'*6}")
    for r in routes:
        click.echo(
            f"  {r['name']:<26} {r['net_id']:<22} "
            f"{r['address']:<18} {r['type']:<8} {r['flags']}"
        )


@tc_target.command("set")
@click.argument("target")
def tc_target_set(target: str):
    """Set the target by name, address, or AMS NetId.

    TARGET is matched against configured static routes
    before being sent to the TwinCAT runtime.
    """
    # Try static routes first
    try:
        route = resolve_target(target)
    except ValueError:
        # Not found in routes — try as raw NetId
        try:
            r = set_target_net_id(target.strip())
            click.echo(f"  Target set to NetId: {r['target_net_id']}")
            click.echo(f"  (not found in StaticRoutes.xml — set as raw NetId)")
            return
        except Exception as e:
            click.echo(
                f"  [ERROR] Target '{target}' not found and could not be set: {e}",
                err=True,
            )
            return

    # Found in routes — set via COM
    try:
        r = set_target_net_id(route["net_id"])
        click.echo(f"  Target set: {route['name']} ({route['net_id']})")
        click.echo(f"  Matched by: {route['matched_by']} (query: '{target}')")
    except Exception as e:
        click.echo(
            f"  [ERROR] Failed to set target '{route['name']}': {e}",
            err=True,
        )


@tc_target.command("add")
@click.option("--name", "-n", default=None, help="Device display name")
@click.option("--address", "-a", default=None, help="IP address or hostname")
@click.option("--net-id", default="", help="AMS NetId (default: {address}.1.1)")
@click.option("--user", default="", help="Remote device Administrator username")
@click.option("--password", default="", help="Remote device Administrator password")
@click.option("--auto-auth", is_flag=True, default=False,
              help="Use default credentials (Administrator / 1) + adapter picker")
def tc_target_add(name: str | None, address: str | None, net_id: str,
                   user: str, password: str, auto_auth: bool):
    """Register a new AMS route through the supported route manager (no direct XML writes).

    \b
    Interactive (recommended):
      tc target add --auto-auth     → pick adapter → scan devices → auto-register

    Direct (skip adapter picker):
      tc target add -n CX-A1 -a 192.168.1.229 --user USER --password PASSWORD
      tc target add -n CX-A1 -a 192.168.1.229 --auto-auth   → add + auth
    """
    if auto_auth:
        user = user or "Administrator"
        password = password or "1"

    # ── Interactive: pick adapter, scan, choose device ──
    if auto_auth and (name is None or address is None):
        adapters = list_local_adapters()
        if not adapters:
            click.echo("  [ERROR] No network adapters found.", err=True)
            return

        click.echo(f"\n  Select network adapter for route:\n")
        for i, a in enumerate(adapters, 1):
            click.echo(f"    [{i}] {a['name']}  ({a['ip']})")
        click.echo()

        choice = click.prompt("  Adapter number", type=int, default=1)
        if choice < 1 or choice > len(adapters):
            click.echo("  [ERROR] Invalid selection", err=True)
            return
        adapter = adapters[choice - 1]
        click.echo(f"  Selected: {adapter['name']} ({adapter['ip']})")

        # Scan the adapter's subnet for devices
        click.echo(f"\n  Scanning {adapter['subnet']} for Beckhoff devices...")
        devices = search_network_targets(subnet=adapter["subnet"], max_hosts=254)

        # Show only remote devices (not self)
        remote_devices = [d for d in devices if d["ip"] != adapter["ip"]]
        if not remote_devices:
            click.echo(f"  No remote Beckhoff devices found on {adapter['name']}.")
            click.echo(f"  Enter target details manually:")
            if address is None:
                address = click.prompt("  IP address")
            if name is None:
                name = click.prompt("  Device name")
        elif len(remote_devices) == 1:
            d = remote_devices[0]
            name = name or d.get("route_name") or d.get("hostname", "").split(".")[0] or d["ip"]
            address = address or d["ip"]
            click.echo(f"  Found: {name} ({address})")
        else:
            click.echo(f"\n  Found {len(remote_devices)} device(s):\n")
            click.echo(f"  {'#':<3} {'Name':<26} {'IP':<18} {'Hostname'}")
            click.echo(f"  {'-'*3} {'-'*26} {'-'*18} {'-'*20}")
            for i, d in enumerate(remote_devices, 1):
                dname = d.get("route_name") or d.get("hostname", "").split(".")[0] or "(unknown)"
                click.echo(
                    f"  [{i}] {dname:<26} {d['ip']:<18} {d.get('hostname', '')}"
                )
            click.echo()
            dev_choice = click.prompt(
                "  Select device [1..n]",
                type=int, default=1,
            )
            if 1 <= dev_choice <= len(remote_devices):
                d = remote_devices[dev_choice - 1]
                name = name or d.get("route_name") or d.get("hostname", "").split(".")[0] or d["ip"]
                address = address or d["ip"]
            else:
                click.echo("  [ERROR] Invalid selection", err=True)
                return

    # ── Fallback: prompt for missing values ──
    if name is None:
        name = click.prompt("  Device name")
    if address is None:
        address = click.prompt("  IP address")
    if not net_id:
        net_id = f"{address}.1.1"

    click.echo(f"  Adding route: {name} ({address} / {net_id})")
    r = add_static_route(name, address, net_id, user=user, password=password)

    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message')}", err=True)
        return

    click.echo(f"  Route added: {r['name']}")
    click.echo(f"    Address:  {r['address']}")
    click.echo(f"    NetId:    {r['net_id']}")

    if r.get("verified"):
        click.echo(f"    Authenticated with target — route is active.")


@tc_target.command("search")
@click.option("--subnet", default="", help="CIDR subnet (e.g. 192.168.1.0/24)")
def tc_target_search(subnet: str):
    """Scan local network for Beckhoff TwinCAT devices.

    Probes TCP port 48898 (AMS Router) on all local subnets.
    Discovered devices are matched against existing StaticRoutes.
    """
    import socket as _socket

    # Show local IPs for reference
    hostname = _socket.gethostname()
    click.echo(f"  Local host: {hostname}")
    click.echo(f"  Scanning for TwinCAT devices...")
    if subnet:
        click.echo(f"  Subnet: {subnet}")

    devices = search_network_targets(subnet=subnet)

    if not devices:
        click.echo(f"  No devices found.")
        click.echo(f"  Ensure AMS Router is running on target devices.")
        return

    # Group by subnet for clarity
    by_subnet: dict[str, list[dict]] = {}
    for d in devices:
        net = ".".join(d["ip"].split(".")[:3]) + ".0/24"
        by_subnet.setdefault(net, []).append(d)

    click.echo(f"")
    click.echo(f"  Found {len(devices)} device(s):")
    click.echo(f"")

    for net, devs in sorted(by_subnet.items()):
        click.echo(f"  [{net}]")
        for d in sorted(devs, key=lambda x: x["ip"]):
            known = d.get("route_name", "")
            marker = "  (in routes)" if known else ""
            host = d.get("hostname", "")
            click.echo(f"    {d['ip']:<16}  {host:<32} {marker}")
        click.echo()

    # Suggest next steps
    unknown = [d for d in devices if not d.get("route_name")]
    if unknown:
        click.echo(f"  {len(unknown)} device(s) not in routes. To add:")
        for d in unknown[:3]:
            name = d.get("hostname", "").split(".")[0] or "Device"
            click.echo(f"    tc target add {name} {d['ip']}")
    click.echo()


@tc_target.command("find")
@click.option("--method", "-m", default="all",
              type=click.Choice(["arp", "ping", "ads", "all"]),
              help="Scan method: arp (MAC lookup), ping (sweep), ads (port 48898), all")
def tc_target_find(method: str):
    """Discover Beckhoff controllers on the local network.

    Uses ARP cache lookup + ping sweep + ADS port probe to find
    Beckhoff devices.  Cross-references with StaticRoutes.xml.

    \\b
    tc target find              # all methods (recommended)
    tc target find -m arp       # ARP only (fastest)
    tc target find -m ping      # ping sweep only
    """
    click.echo(f"  Scanning for Beckhoff devices (method: {method})...")
    devices = find_beckhoff_devices(scan_method=method)

    if not devices:
        click.echo()
        click.echo("  No Beckhoff devices found.")
        click.echo("  ── Check power and Ethernet connection.")
        click.echo("  ── Try 'tc target search' for a broader ADS scan.")
        click.echo("  ── If the device is on a different subnet, specify it manually:")
        click.echo("      tc target search 192.168.1.0/24")
        return

    click.echo()
    click.echo(f"  Found {len(devices)} device(s):")
    click.echo()

    for d in devices:
        ip = d["ip"]
        mac = d.get("mac", "")
        route = d.get("route_name", "")
        method_tag = d.get("method", "")
        extra = ""
        if mac:
            extra += f"  MAC={mac}"
        if route:
            extra += f"  route={route}"
        if method_tag:
            extra += f"  [{method_tag}]"
        click.echo(f"    {ip}{extra if extra else ''}")

    click.echo()
    unknown = [d for d in devices if not d.get("route_name")]
    if unknown:
        click.echo(f"  {len(unknown)} device(s) not in routes. To add:")
        for d in unknown:
            name = d.get("route_name") or f"CX-{d['ip'].replace('.', '-')}"
            click.echo(f"    tc target add {name} {d['ip']} --auto-auth")
    click.echo()


@cmd_tc.command("boot")
def tc_boot():
    """Set PLC project as boot project (autostart)."""
    r = set_boot_project()
    click.echo(f"  Boot project: {r}")


@cmd_tc.command("deploy")
@click.option("--no-silent", is_flag=True, default=False,
              help="Show trial-license reminders and other TwinCAT dialogs")
def tc_deploy(no_silent: bool):
    """Full deploy: build → activate → restart → login → start.

    Does not switch to Config mode.  Config mode is only for an explicit mode
    change or hardware scan preparation.

    By default, SilentMode suppresses license warnings.  Use --no-silent
    if you need to see trial-license dialogs.
    """
    use_silent = not no_silent
    if not use_silent:
        click.echo("  (silent mode off — license dialogs will appear)")

    # Show platform before deploying
    plat = get_build_platform()
    if plat.get("status") == "ok":
        click.echo(f"  Platform: {plat['full']}")

    click.echo("  Building...")
    r = deploy(silent=use_silent)
    if "platform" in r:
        platform_result = r["platform"]
        click.echo(
            f"  Platform: {platform_result.get('status', '?')}  "
            f"{platform_result.get('full') or platform_result.get('error', '?')}"
        )
    if "build_config" in r and not r["build_config"].get("verified"):
        click.echo(f"  Build config: FAIL  {r['build_config']}")
    if "build" in r:
        click.echo(f"  Build errors: {r['build'].get('error_count', 0)}")
        if r["build"].get("errors"):
            for error in r["build"]["errors"]:
                click.echo(f"    {error}")
    # Print intermediate steps so failures aren't hidden
    for step in ["boot", "activate", "restart"]:
        if step in r:
            s = r[step]
            if isinstance(s, dict):
                ok = (s.get("boot_project") or
                      s.get("status") in ("ok", "activated", "restarted"))
                label = "OK" if ok else "FAIL"
                click.echo(f"  {step.capitalize()}: {label}  {s}")
            else:
                click.echo(f"  {step.capitalize()}: {s}")
    if "system" in r:
        click.echo(f"  System state: {r['system']}")
    if "online" in r:
        click.echo(f"  Online: {r['online']}")
    if not r.get("success", False):
        stage = r.get("failed_stage", "unknown")
        raise click.ClickException(f"Deploy failed at stage: {stage}")
    click.echo("  Deploy: OK (system and all PLC runtimes verified)")


# ------------------------------------------------------------------
# platform — build platform management
# ------------------------------------------------------------------

@cmd_tc.group("platform")
def tc_platform():
    """Build platform: show, list, set (Debug/Release × TwinCAT RT/OS)."""


@tc_platform.command("show")
def tc_platform_show():
    """Show the currently active build platform."""
    r = com_get_build_platform()
    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Unknown')}", err=True)
        return
    click.echo(f"  Platform: {r['full']}")
    click.echo(f"    Config:   {r['config']}")
    click.echo(f"    Platform: {r['platform']}")


@tc_platform.command("list")
def tc_platform_list():
    """List all available build platforms in the open solution."""
    r = com_list_build_platforms()
    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Unknown')}", err=True)
        return

    active_full = r["active"]
    for p in r["platforms"]:
        marker = " *" if p["full"] == active_full else "  "
        click.echo(f"  [{p['index']:2d}]{marker} {p['full']}")


@tc_platform.command("set")
@click.argument("platform", required=False)
@click.option("--config", "-c", default=None, help="Build config: Debug or Release")
def tc_platform_set(platform: str | None, config: str | None):
    """Set the build platform.

    PLATFORM: Target platform, e.g. 'TwinCAT RT (x64)' or 'TwinCAT RT (x86)'.
    If omitted, auto-detects based on the current target controller.

    \b
    Examples:
      tc platform set "TwinCAT RT (x64)"
      tc platform set "TwinCAT RT (x86)" -c Debug
      tc platform set                    (auto-detect)
    """
    if not platform:
        r = set_build_platform(platform="", config=config or "")
    else:
        r = com_set_build_platform(platform=platform, config=config or "")
    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Unknown')}", err=True)
        avail = r.get("available", [])
        if avail:
            click.echo(f"  Available:")
            for a in avail:
                click.echo(f"    - {a}")
        return
    click.echo(f"  Switched to: {r['full']}")


# ------------------------------------------------------------------
# version — TcVersion management
# ------------------------------------------------------------------

@cmd_tc.group("version")
def tc_version():
    """TwinCAT version: show, pin, unpin for the open project."""


@tc_version.command("show")
def tc_version_show():
    """Show current target and local TwinCAT version info."""
    try:
        target_ver = get_target_tc_version()
        click.echo(f"  Target TcVersion:  {target_ver['version_str']}")
    except Exception:
        click.echo(f"  Target TcVersion:  (no project open)")

    local_ver = get_local_tc_version()
    click.echo(f"  Local TcVersion:   {local_ver['version_str']}")

    try:
        pin = get_pinned_tc_version()
        if pin["pinned_version"]:
            status = "FIXED" if pin["is_fixed"] else "pinned"
            click.echo(f"  Pinned TcVersion:  {pin['pinned_version']} ({status})")
        else:
            click.echo(f"  Pinned TcVersion:  (not pinned)")
    except Exception:
        click.echo(f"  Pinned TcVersion:  (no project open)")


@tc_version.command("pin")
@click.argument("version", required=False)
def tc_version_pin(version: str | None):
    """Pin the project to a specific TwinCAT version.

    If VERSION is omitted, uses the target's current TcVersion.
    """
    if version is None:
        ver_info = get_target_tc_version()
        version = ver_info["version_str"]

    r = set_pin_tc_version(version, fixed=True)
    click.echo(f"  Pinned:  {r['pinned_version']}")
    click.echo(f"  Fixed:   {r['is_fixed']}")


@tc_version.command("unpin")
def tc_version_unpin():
    """Remove the pinned TcVersion (let TwinCAT auto-select)."""
    r = set_pin_tc_version("", fixed=False)
    click.echo(f"  Unpinned — TwinCAT will auto-select version.")


@cmd_tc.command("scan")
def tc_scan():
    """Scan the current target for EtherCAT devices and add them to the configuration.

    The target device must already be in Config mode. This command never
    changes runtime mode, activates configuration, or restarts TwinCAT.
    """
    # Show target info upfront
    netid = get_target_net_id()
    click.echo(f"  Target: {netid}")
    click.echo(f"  Checking target Config mode (no automatic switch)...")
    r = scan_devices()

    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Unknown error')}", err=True)
        return

    found = r.get("found", [])
    added = r.get("added", [])
    boxes = r.get("boxes", {})
    skipped = r.get("skipped", [])
    target = r.get("target", netid)

    if not found:
        tries = r.get("config_tries", 0)
        if tries >= 2:
            click.echo(f"  Config mode attempted {tries} times — still no devices found.")
            click.echo(f"  Check EtherCAT cable, controller power, and target NetId.")
        else:
            click.echo(f"  Target {target}: {r.get('message', 'No devices found.')}")
        return

    click.echo(f"  Target {target} is in Config mode, scanning hardware...")
    click.echo(f"\n  Found {len(found)} device(s):")
    for name in found:
        simple_name = name.split(" (")[0]  # strip error details for matching
        was_skipped = any(s.startswith(simple_name) for s in skipped)
        was_added = name in added
        if was_skipped:
            status = "s"  # skipped — already configured
        elif was_added:
            status = "+"  # newly added
        else:
            status = "?"  # unexpected
        box_count = boxes.get(name, "?")
        click.echo(f"    [{status}] {name}  (slaves: {box_count})")

    if skipped:
        click.echo(f"\n  {len(skipped)} device(s) skipped (already configured):")
        for s in skipped:
            click.echo(f"    [s] {s}")

    if any("failed" in a for a in added):
        failed = [a for a in added if "failed" in a]
        click.echo(f"\n  {len(failed)} device(s) failed to add:")
        for f in failed:
            click.echo(f"    [!] {f}")


# ------------------------------------------------------------------
# link — variable linking (I/O <-> PLC)
# ------------------------------------------------------------------

@cmd_tc.group("link")
def tc_link():
    """Variable linking: list I/O variables, show/create/remove links."""


@tc_link.command("list")
def tc_link_list():
    """List I/O process-data variables available for linking."""
    netid = get_target_net_id()
    click.echo(f"  Target: {netid}")
    r = list_io_variables()

    if r.get("message"):
        click.echo(f"  {r['message']}")
        return

    click.echo(f"  Device: {r['device']}")
    click.echo(f"  Process-data variables: {r['total_inputs']} inputs, "
               f"{r['total_outputs']} outputs\n")

    for term in r.get("terminals", []):
        # Count channels to give a hint about the terminal type
        in_ch = len(term["inputs"])
        out_ch = len(term["outputs"])
        if in_ch and out_ch:
            kind = f"{in_ch} in / {out_ch} out"
        elif in_ch:
            kind = f"{in_ch}-ch digital input"
        else:
            kind = f"{out_ch}-ch digital output"
        click.echo(f"  -- {term['name']}  ({kind}) --")

        for label, lst in [("Inputs", term["inputs"]), ("Outputs", term["outputs"])]:
            if not lst:
                continue
            click.echo(f"    [{label}]")
            for v in lst:
                chan = f" ({v['channel']})" if v["channel"] else ""
                click.echo(f"      {v['name']}{chan}")
        click.echo()


@tc_link.command("show")
def tc_link_show():
    """Show current variable mappings (linked I/O <-> PLC pairs)."""
    r = get_mapping_info()

    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message')}", err=True)
        return

    links = r.get("links", [])
    if not links:
        click.echo("  No variable links configured.")
        return

    click.echo(f"  Target: {r['target']}")
    click.echo(f"  {len(links)} link(s):\n")
    for i, link in enumerate(links, 1):
        click.echo(f"  [{i}] {link['owner_a']}^{link['var_a']}")
        click.echo(f"      <-> {link['owner_b']}^{link['var_b']}")
        if link["size"]:
            click.echo(f"      (Size={link['size']}, OffsA={link['offs_a']}, "
                       f"OffsB={link['offs_b']})")


@tc_link.command("add")
@click.argument("io_path")
@click.argument("plc_path")
def tc_link_add(io_path: str, plc_path: str):
    """Link an I/O variable to a PLC symbol.

    IO_PATH is the full tree path to the I/O process-data variable
    (as shown by 'tc link list').

    PLC_PATH is the full tree path to the PLC variable.
    Example: TIPC^MyPlc^MyPlc Instance^PlcTask Inputs^MAIN.bSensor1
    """
    r = link_variable(io_path, plc_path)
    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Link failed')}", err=True)
        return
    if r.get("already_linked"):
        click.echo(f"  Already linked (no change):")
    else:
        click.echo(f"  Linked:")
    click.echo(f"    I/O:  {io_path}")
    click.echo(f"    PLC:  {plc_path}")


@tc_link.command("remove")
@click.argument("io_path")
@click.option("--plc-path", "-p", default="",
              help="PLC path to unlink (omit to clear ALL links from I/O var)")
def tc_link_remove(io_path: str, plc_path: str):
    """Remove variable link(s) from an I/O variable."""
    r = unlink_variable(io_path, plc_path)
    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message', 'Unlink failed')}", err=True)
        return
    click.echo(f"  Unlinked:")
    click.echo(f"    I/O:  {io_path}")
    click.echo(f"    PLC:  {r['plc_path']}")


@cmd_tc.group("nc")
def tc_nc():
    """TwinCAT NC configuration, links, parameters, and diagnostics."""


@tc_nc.command("structure")
@click.option("--depth", default=6, type=int)
def tc_nc_structure(depth: int):
    click.echo(json.dumps(nc_structure(depth), ensure_ascii=False, indent=2))


@tc_nc.command("axis-info")
@click.argument("axis")
def tc_nc_axis_info(axis: str):
    click.echo(json.dumps(nc_axis_info(axis), ensure_ascii=False, indent=2))


@tc_nc.command("axis-params")
@click.argument("axis")
def tc_nc_axis_params(axis: str):
    click.echo(json.dumps(nc_axis_params(axis), ensure_ascii=False, indent=2))


@tc_nc.command("axis-params-set")
@click.argument("axis")
@click.argument("parameters")
def tc_nc_axis_params_set(axis: str, parameters: str):
    """Set parameters from a JSON object, then verify by reading them back."""
    values = json.loads(parameters)
    click.echo(json.dumps(nc_set_axis_params(axis, values), ensure_ascii=False, indent=2))


@tc_nc.command("create-task")
@click.argument("name", default="NC-Task")
def tc_nc_create_task(name: str):
    click.echo(json.dumps(nc_create_task(name), ensure_ascii=False, indent=2))


@tc_nc.command("create-axis")
@click.argument("task")
@click.argument("name")
@click.option("--type", "axis_type", default="continuous",
              type=click.Choice(["continuous", "virtual"]))
def tc_nc_create_axis(task: str, name: str, axis_type: str):
    click.echo(json.dumps(nc_create_axis(task, name, axis_type), ensure_ascii=False, indent=2))


@tc_nc.command("drive-list")
def tc_nc_drive_list():
    click.echo(json.dumps(find_servo_drives(), ensure_ascii=False, indent=2))


@tc_nc.command("encoder-list")
def tc_nc_encoder_list():
    click.echo(json.dumps(find_nc_encoders(), ensure_ascii=False, indent=2))


@tc_nc.command("link-drive")
@click.argument("axis")
@click.argument("drive_path")
@click.option("--channel", type=int, default=None, help="Drive channel (required for multi-channel AX drives)")
def tc_nc_link_drive(axis: str, drive_path: str, channel: int | None):
    click.echo(json.dumps(nc_link_drive(axis, drive_path, channel), ensure_ascii=False, indent=2))


@tc_nc.command("link-encoder")
@click.argument("axis")
@click.argument("encoder_path")
def tc_nc_link_encoder(axis: str, encoder_path: str):
    click.echo(json.dumps(nc_link_encoder(axis, encoder_path), ensure_ascii=False, indent=2))


@tc_nc.command("links")
def tc_nc_links():
    click.echo(json.dumps(nc_links(), ensure_ascii=False, indent=2))


@tc_nc.command("state")
def tc_nc_state():
    click.echo(json.dumps(get_nc_state(), ensure_ascii=False, indent=2))


@tc_nc.command("axis-state")
@click.argument("axis")
def tc_nc_axis_state(axis: str):
    click.echo(json.dumps(nc_axis_state(axis), ensure_ascii=False, indent=2))


@tc_nc.command("axis-move")
@click.argument("axis")
@click.argument("position", type=float)
@click.argument("velocity", type=float)
@click.option("--timeout", type=float, default=30.0)
@click.option("--control-scope", default="")
@click.option("--confirm-physical", is_flag=True)
@click.option("--allow-unhomed", is_flag=True)
def tc_nc_axis_move(axis: str, position: float, velocity: float, timeout: float,
                    control_scope: str, confirm_physical: bool, allow_unhomed: bool):
    click.echo(json.dumps(nc_axis_move(
        axis, position, velocity, timeout, control_scope,
        confirm_physical, not allow_unhomed,
    ), ensure_ascii=False, indent=2))


@tc_nc.command("validate")
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False))
def tc_nc_validate(manifest: str):
    import json as _json
    data = _json.loads(Path(manifest).read_text(encoding="utf-8"))
    click.echo(json.dumps(nc_validate(data), ensure_ascii=False, indent=2))


@cmd_tc.command("ncConfigurate")
def tc_nc_configurate():
    """Create NC task and axes for discovered servo drives.

    Finds servo drive terminals (e.g., EL7201, AX5206) in the I/O tree,
    creates an NC task under TINC if one does not exist, then creates one
    axis per drive and links it to the corresponding EtherCAT terminal.
    """
    netid = get_target_net_id()
    click.echo(f"  Target: {netid}")
    click.echo(f"  Searching for servo drives...")

    # Show what drives were found
    drives = find_servo_drives()
    if not drives:
        click.echo(f"  No servo drives found in I/O tree.")
        click.echo(f"  Run 'tc scan' first, then ensure servo terminals are present.")
        return

    click.echo(f"  Found {len(drives)} drive(s):")
    for d in drives:
        pdos_str = ", ".join(d["pdos"][:4])
        click.echo(f"    - {d['name']}  ({d.get('subtype_name', '')})")
        click.echo(f"      PDOs: {pdos_str}")

    click.echo(f"\n  Creating NC task and axes...")
    r = create_nc_task_and_axes()

    if r["status"] == "error":
        click.echo(f"  [ERROR] {r.get('message')}", err=True)
        return

    if r.get("message"):
        click.echo(f"  {r['message']}")
        return

    click.echo(f"  NC Task: {r['nc_task']}")

    ok = failed = skipped = 0
    for ax in r["axes"]:
        if ax["status"] == "ok":
            ok += 1
            click.echo(f"    [+] {ax['name']}  <-> {ax['drive']}")
        elif "skipped" in ax.get("status", ""):
            skipped += 1
            click.echo(f"    [s] {ax['name']}  ({ax.get('reason', 'skipped')})")
        else:
            failed += 1
            click.echo(f"    [!] {ax['name']}  ({ax.get('reason', 'failed')})")

    click.echo(f"\n  Done: {ok} created, {skipped} skipped, {failed} failed")


@cmd_tc.command("silent")
@click.option("--on/--off", default=True)
def tc_silent(on: bool):
    """Enable/disable silent mode (suppress dialogs)."""
    r = set_silent_mode(on)
    click.echo(f"  Silent mode: {r['silent_mode']}")


@cmd_tc.command("close")
def tc_close():
    """Close the currently open solution (save all, then close)."""
    r = close_solution()
    if r["status"] == "closed":
        click.echo("  Solution closed.")
    elif r["status"] == "skipped":
        click.echo(f"  Skipped: {r.get('reason', '')}")
    else:
        click.echo(f"  Error: {r.get('reason', '')}")


@cmd_tc.command("quit")
def tc_quit():
    """Quit the XAE application window (save + close solution, then exit)."""
    r = quit_xae()
    if r["status"] == "quit":
        click.echo("  XAE closed.")
    elif r["status"] == "skipped":
        click.echo(f"  Skipped: {r.get('reason', '')}")
    else:
        click.echo(f"  Error: {r.get('reason', '')}")


@cmd_tc.command("open")
@click.argument("sln_path", type=click.Path(exists=True))
def tc_open(sln_path: str):
    """Open a .sln file. Closes any currently open solution first."""
    r = open_solution(sln_path)
    if r["status"] == "opened":
        click.echo(f"  Opened: {r['path']}")
        if "warning" in r:
            click.echo(f"  Warning: {r['warning']}")
    else:
        click.echo(f"  Error: {r.get('reason', '')}")


# ======================================================================
# diag — TwinCAT Diagnostics
# ======================================================================

@main.group("diag")
def cmd_diag():
    """Diagnostics: health check, ADS ping, route test, error analysis."""


@cmd_diag.command("health")
@click.option("--no-errors", is_flag=True, default=False,
              help="Skip the error list check")
def diag_health(no_errors: bool):
    """Run a comprehensive system health check.

    Checks: TwinCAT runtime, XAE connectivity, solution/project status,
    target connectivity (ADS ping), routes, build platform, version,
    and optionally the error list.
    """
    from .diagnostics import system_health_check, format_health_report

    click.echo("\n  Running system health check...\n")
    report = system_health_check(include_error_list=not no_errors)
    click.echo(format_health_report(report))


@cmd_diag.command("ping")
@click.argument("target", required=False)
def diag_ping(target: str):
    """Test ADS connectivity to a target NetId.

    \b
    TARGET: Optional AMS NetId. Uses current target if omitted.
    Example: tc diag ping 172.16.1.100.1.1
    """
    from .diagnostics import ads_ping

    click.echo(f"\n  Pinging {'current target' if not target else target}...")
    r = ads_ping(target=target or "")

    if r["reachable"]:
        click.echo(f"  ✅ Reachable  ({r['latency_ms']} ms via {r['method']})")
        click.echo(f"     Target: {r['target']}")
    else:
        click.echo(f"  ❌ NOT reachable")
        click.echo(f"     Target: {r['target']}")
        if r.get("error"):
            click.echo(f"     Error: {r['error'][:200]}")


@cmd_diag.command("route")
@click.argument("target", required=False)
def diag_route(target: str):
    """Run diagnostics on a specific route.

    \b
    TARGET: AMS NetId or route name. Uses current target if omitted.
    Verifies: route exists, ADS ping, runtime state.
    """
    from .diagnostics import route_diagnostics, format_route_report

    click.echo(f"\n  Running route diagnostics...\n")
    report = route_diagnostics(target=target or "")
    click.echo(format_route_report(report))


@cmd_diag.command("target")
@click.argument("target", required=False)
def diag_target_info(target: str):
    """Show comprehensive information about a target.

    \b
    Reads: route info, ADS device name, runtime state, CPU type,
    OS version, TwinCAT version, and more.
    """
    from .diagnostics import target_info, format_target_info

    click.echo(f"\n  Querying target information...\n")
    info = target_info(target=target or "")
    click.echo(format_target_info(info))


@cmd_diag.command("errors")
def diag_errors():
    """Read error list with enhanced diagnosis and fix suggestions.

    Maps common TwinCAT compile errors to likely causes and solutions.
    """
    from .diagnostics import diagnose_errors

    click.echo(f"\n  Reading error list + running diagnosis...\n")
    r = diagnose_errors()

    if not r.get("success"):
        click.echo(f"  {r.get('message', 'Could not read error list')}")
        return

    if r["total"] == 0:
        click.echo("  ✅ No errors, warnings, or messages.  Project is clean.")
        return

    click.echo(f"  Found {r['total']} items "
               f"({r['error_count']} err, {r['warning_count']} warn, "
               f"{r['info_count']} info)\n")

    for e in r["errors"]:
        sev = e["severity"]
        symbol = {"Error": "❌", "Warning": "⚠️", "Info": "ℹ️"}.get(sev, "")
        click.echo(f"  {symbol} [{sev}] {e['description'][:150]}")
        if e.get("file"):
            click.echo(f"       File: {e['file']}:{e.get('line', '')}")
        if e.get("diagnosis"):
            click.echo(f"       💡 {e['diagnosis'][:200]}")
        click.echo()


@cmd_diag.command("license")
def diag_license():
    """Show TwinCAT license status."""
    from .diagnostics import license_status

    click.echo("\n  Querying license status...\n")
    r = license_status()

    if r.get("error"):
        click.echo(f"  ❌ {r['error']}")
        return

    if "device_name" in r:
        click.echo(f"  Device: {r['device_name']}")
    if "islicensed" in r:
        click.echo(f"  Licensed: {r['islicensed']}")
    if "licenseinfo" in r:
        click.echo(f"  License: {r['licenseinfo'][:200]}")
    if "triallicensedaysleft" in r:
        click.echo(f"  Trial days left: {r['triallicensedaysleft']}")
    if "tcversion" in r:
        click.echo(f"  TcVersion: {r['tcversion']}")

    if r.get("note"):
        click.echo(f"\n  ℹ️ {r['note']}")


# ======================================================================
# fblib — FB 库：常用功能块模板 列出/详情/塞入/抽取/校验
# ======================================================================

@main.group("fblib")
def cmd_fblib():
    """FB 库：把常用功能块封装成模板，一键塞入/反向抽取（见 docs/fblib_architecture.md）。"""


@cmd_fblib.command("list")
@click.argument("category", required=False)
def fblib_list_cmd(category: str | None):
    """按分类列出 FB 库（motion/framework/device/communication/tools）。"""
    from .fblib import list_fbs, load_catalog
    items = list_fbs(category)
    if not items:
        click.echo("  (FB 库为空或无匹配)"); return
    labels = {c["name"]: c for c in load_catalog().get("categories", []) or []}
    cur = None
    for f in items:
        if f["category"] != cur:
            cur = f["category"]
            c = labels.get(cur, {})
            click.echo(f"\n== {cur} {c.get('label', '')} — {c.get('summary', '')}")
        fam = "SPT" if f.get("family") == "spt-framework" else "独立"
        click.echo(f"  {f['slug']:<22} [{fam}] {f['description']}")
        if f.get("use_when"):
            click.echo(f"  {'':<22} 何时用: {f['use_when']}")


@cmd_fblib.command("find")
@click.argument("intent", nargs=-1, required=True)
def fblib_find_cmd(intent: tuple):
    """按意图选模板：fblib find 我要写轴程序 → 首选 spt-axis-basic。"""
    from .fblib import find_fbs
    r = find_fbs(" ".join(intent))
    click.echo(f"  意图: {r['intent']}")
    if r["matched_categories"]:
        click.echo(f"  命中分类: {', '.join(r['matched_categories'])}")
    for i, f in enumerate(r["results"], 1):
        mark = "★ 首选" if i == 1 else f"  {i}."
        fam = "SPT" if f.get("family") == "spt-framework" else "独立"
        click.echo(f"  {mark} {f['slug']:<22} [{fam}] (score {f['score']}) {f['why']}")
        click.echo(f"        {f['description']}")
    click.echo(f"  → {r['hint']}")


@cmd_fblib.command("info")
@click.argument("slug")
def fblib_info_cmd(slug: str):
    """查看模板 manifest（参数/依赖库/配套类型/原型）。"""
    import json
    from .fblib import get_fb
    try:
        click.echo(json.dumps(get_fb(slug)["manifest"], ensure_ascii=False, indent=2))
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True)


@cmd_fblib.command("add")
@click.argument("slug")
@click.option("--param", "-P", "params", multiple=True, help="K=V 参数覆盖，可多次")
def fblib_add_cmd(slug: str, params: tuple):
    """把模板塞进当前打开的 PLC 项目（建 FB+配套类型+加库+lint）。"""
    from .fblib import add_fb
    try:
        pd = dict(p.split("=", 1) for p in params)
    except ValueError:
        raise click.BadParameter("需使用 K=V 形式", param_hint="--param")
    try:
        r = add_fb(slug, pd)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  Added FB: {r['fb']}")
    if r["companions"]:
        click.echo(f"    companions: {', '.join(r['companions'])}")
    if r["libraries"]:
        click.echo(f"    libraries:  {', '.join(r['libraries'])}")
    lint = r.get("lint", {}).get("summary", {})
    click.echo(f"    lint: {lint.get('total', 0)} issue(s)")
    if r.get("status") != "added" or r.get("library_warnings"):
        warnings = "; ".join(r.get("library_warnings", [])) or r.get("status", "unknown")
        raise click.ClickException(f"FB was only partially added: {warnings}")


@cmd_fblib.command("extract")
@click.argument("fb_name")
@click.argument("slug")
@click.option("--category", "-c", default="", help="communication/motion/tools/device")
@click.option("--desc", "-d", default="", help="一句话描述")
@click.option("--companion", "comps", multiple=True, help="配套 DUT 名，可多次")
def fblib_extract_cmd(fb_name: str, slug: str, category: str, desc: str, comps: tuple):
    """从打开的项目反向抽取一个 FB 成模板。"""
    from .fblib import extract_fb
    try:
        r = extract_fb(fb_name, slug, category=category, description=desc,
                       companions=list(comps))
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  Extracted → {r['path']}  (archetype={r['archetype']})")
    click.echo(f"  TODO: {r['todo']}")


@cmd_fblib.command("validate")
@click.argument("slug")
def fblib_validate_cmd(slug: str):
    """离线校验模板（渲染后跑 lint，不需 XAE）。"""
    from .fblib import validate_fb
    try:
        r = validate_fb(slug)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    s = r.get("summary", {})
    click.echo(f"  Validate {slug}: {s.get('total', 0)} issue(s)")
    for f in r.get("findings", []):
        click.echo(f"    [{f['rule']}] {f['object']}: {f['message']}")
    if int(s.get("total", 0) or 0):
        raise click.ClickException(
            f"FB validation failed with {s['total']} issue(s)"
        )


# ======================================================================
# case — 可扩展客户案例库：扫描/搜索/校验/一键导入与更新
# ======================================================================

@main.group("case")
def cmd_case():
    """客户案例库：递归扫描 CaseLibrary，导入 PLCopen XML 到当前 XAE。"""


@cmd_case.command("list")
@click.argument("category", required=False)
def case_list_cmd(category: str | None):
    """列出内置及客户目录中的案例。"""
    from .case_library import discover_cases
    cases = discover_cases()
    if category:
        cases = [c for c in cases if c.get("category", "").lower() == category.lower()]
    if not cases:
        click.echo("  (没有发现案例)"); return
    current = None
    for item in cases:
        group = f"{item.get('publisher', '')}/{item.get('category', '')}"
        if group != current:
            current = group
            click.echo(f"\n== {group}")
        maturity = item.get("maturity", "unset")
        click.echo(f"  {item['id']:<38} v{item.get('version', '?'):<8} [{maturity}]")
        click.echo(f"    {item.get('name', '')} — {item.get('description', '')}")


@cmd_case.command("find")
@click.argument("intent", nargs=-1, required=True)
def case_find_cmd(intent: tuple):
    """按名称、描述和关键词查找案例。"""
    from .case_library import find_cases
    results = find_cases(" ".join(intent))
    if not results:
        click.echo("  (没有匹配案例)"); return
    for index, item in enumerate(results, 1):
        click.echo(f"  {index}. {item['id']}  score={item['score']}  {item['why']}")
        click.echo(f"     {item.get('description', '')}")


@cmd_case.command("info")
@click.argument("case_id")
def case_info_cmd(case_id: str):
    """查看案例 manifest、来源目录和校验状态。"""
    import json
    from .case_library import get_case
    try:
        click.echo(json.dumps(get_case(case_id), ensure_ascii=False, indent=2))
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_case.command("validate")
@click.argument("case_id", required=False)
def case_validate_cmd(case_id: str | None):
    """校验一个案例；省略 ID 时校验全部案例。"""
    from .case_library import discover_cases, get_case, validate_case_dir
    try:
        items = [get_case(case_id)] if case_id else discover_cases(include_invalid=True)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    failed = 0
    for item in items:
        result = validate_case_dir(__import__("pathlib").Path(item["path"]))
        mark = "OK" if result["valid"] else "FAIL"
        click.echo(f"  [{mark}] {item['id']}")
        for error in result["errors"]:
            click.echo(f"    - {error}")
        failed += 0 if result["valid"] else 1
    click.echo(f"  {len(items) - failed} passed, {failed} failed")


def _case_install(case_id: str, replace: bool):
    from .case_library import install_case
    try:
        result = install_case(case_id, replace=replace, build=True)
    except Exception as e:
        click.echo(f"[ERROR] {e}", err=True); return
    click.echo(f"  {result['mode'].title()}: {result['id']} v{result['version']}")
    if result["libraries_added"]:
        click.echo(f"  Libraries: {', '.join(result['libraries_added'])}")
    build = result.get("build", {})
    errors = build.get("errors", build.get("error_count", 0))
    click.echo(f"  Build errors: {errors}")


@cmd_case.command("add")
@click.argument("case_id")
def case_add_cmd(case_id: str):
    """一键导入 reusable 案例；同名对象跳过。"""
    _case_install(case_id, replace=False)


@cmd_case.command("update")
@click.argument("case_id")
def case_update_cmd(case_id: str):
    """一键更新 reusable 案例；同名对象替换。"""
    _case_install(case_id, replace=True)


# ======================================================================
# Entry
# ======================================================================

@cmd_hmi.command('events')
@click.argument('file')
@click.argument('control_id')
@click.option('--project', default='')
@click.option('--event', default='')
@click.option('--action', type=click.Choice(['read', 'upsert', 'remove']), default='read')
@click.option('--placement', type=click.Choice(['native', 'custom']), default='native', show_default=True,
              help='Write declared native events in the lower XAE event groups; custom is explicit only.')
@click.option('--actions-file', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--apply', is_flag=True)
def hmi_events(file, control_id, project, event, action, placement, actions_file, apply):
    """Inspect installed events or preview/edit one control's Trigger actions."""
    from .hmi_events import control_events
    actions = json.loads(actions_file.read_text(encoding='utf-8-sig')) if actions_file else []
    try:
        result = control_events(file, control_id, project, event, actions, action, apply, placement)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
