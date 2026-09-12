"""
tc_template.mcp_server — TwinCAT Agent tools exposed over MCP (stdio).

This is the "tc-mcp" server: the equivalent of Beckhoff coAgent's sysman-mcp,
built on this repo's own tc_template command layer.

Design:
  * COM-touching tools  -> delegated to Windows PowerShell (TcCom.ps1) via
    tc_template._ps_bridge. Robust IDispatch late-binding against the running
    XAE DTE; avoids Python 3.14 / pywin32 COM fragility.
  * Non-COM tools (templates, and later deploy/diag) -> call tc_template Python
    directly. No XAE required.

Run:
    py -3.14 -m tc_template.mcp_server        # stdio server for an MCP host

Any MCP host (Claude Code, Cursor, or a future in-XAE plugin backend) can mount
it. Register in .mcp.json (see repo root).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import _ps_bridge as ps
from .lint import review_write_candidate
from . import repository

mcp = FastMCP("tc-mcp")


# =====================================================================
#  COM tools — routed through PowerShell (TcCom.ps1). Require running XAE.
# =====================================================================

@mcp.tool()
def tc_connect_check() -> dict:
    """Verify the bridge can attach to a running TwinCAT XAE (TcXaeShell / VS).

    Returns the open solution path and DTE name, or raises if no XAE is running.
    Use this first to confirm the environment before other COM tools.
    """
    return ps.com_connect_check()


@mcp.tool()
def plc_list() -> list[dict]:
    """List all PLC objects (POUs / DUTs / GVLs / Interfaces) in the open project.

    Each item: {name, folder, itemType}. Requires a running XAE with a PLC project.
    """
    return ps.com_list()


@mcp.tool()
def plc_read(name: str, method: str = "", member_type: str = "",
             path: str = "", area: str = "all",
             include_member_code: bool = True) -> dict:
    """Read a PLC object's declaration + implementation via COM.

    Args:
        name: object name, e.g. "MAIN" or "FB_Motor".
        method: optional member path, including Action/Transition members.
        member_type: optional method/action/property/transition type hint.
        path: optional exact POU tree path returned by ``plc_find``.
        area: all, declaration, implementation, or members.
        include_member_code: include code for all members when reading the POU.
    Returns:
        {name, itemType, declaration, implementation}.
    """
    if area not in {"all", "declaration", "implementation", "members"}:
        raise ValueError("area must be all, declaration, implementation, or members")
    return ps.com_read_pou(
        name, area=area, method=method, member_type=member_type,
        include_member_code=include_member_code, path=path,
    )


@mcp.tool()
def plc_write(name: str, code: str, area: str = "implementation") -> str:
    """Review then write code into a PLC object via COM (IDE reparses automatically, no dialog).

    Args:
        name: target object, e.g. "MAIN".
        code: full replacement text for the chosen area.
        area: "declaration" or "implementation" (default).
    """
    if area not in ("declaration", "implementation"):
        raise ValueError("area must be 'declaration' or 'implementation'")
    return ps.com_write_pou(name, code, area=area)


@mcp.tool()
def plc_review(name: str) -> dict:
    """Read-only review of a PLC object's full post-edit quality contract.

    Reports declaration ordering, documentation/range coverage, transaction FB
    contract, state machine and child-FB call placement. The same blocking
    findings prevent ``plc_write`` and ``plc_create`` from changing XAE.
    """
    current = ps.com_read_pou(name)
    candidate = {
        "name": current.get("name") or name, "folder": "POUs",
        "declaration": current.get("declaration") or "",
        "implementation": current.get("implementation") or "",
        "methods": current.get("methods") or [],
    }
    return review_write_candidate(candidate, changed_area="all")


@mcp.tool()
def plc_create(name: str, pou_type: str = "fb",
               declaration: str = "", implementation: str = "",
               return_type: str = "", path: str = "") -> str:
    """Create a new PLC object via COM CreateChild, optionally with initial code.

    Args:
        name: new object name.
        pou_type: one of fb | program | function | struct | enum | union | alias | gvl | interface.
        declaration: optional initial declaration text.
        implementation: optional initial implementation text.
        return_type: required for a standalone function (for example ``BOOL``).
        path: optional exact target PLC folder path; omitted uses the standard
            category folder.
    """
    valid = {"fb", "program", "function", "struct", "enum", "union", "alias", "gvl", "interface"}
    if pou_type not in valid:
        raise ValueError(f"pou_type must be one of {sorted(valid)}")
    return ps.com_new_pou(name, pou_type=pou_type,
                          declaration=declaration, implementation=implementation,
                          return_type=return_type, path=path)


@mcp.tool()
def plc_create_folder(name: str, parent_path: str = "") -> dict:
    """Create a PLC tree folder below an exact parent node via COM."""
    return ps.com_create_folder(name, parent_path=parent_path)


@mcp.tool()
def plc_tree(max_nodes: int = 2500) -> dict:
    """Return the live PLC tree, including empty folders and exact paths."""
    return ps.com_plc_tree(max_nodes=max_nodes)


@mcp.tool()
def plc_create_member(pou: str, name: str, member_type: str = "method",
                      return_type: str = "BOOL", language: str = "ST",
                      declaration: str = "", implementation: str = "") -> dict:
    """Create a member (method/property/action/transition/getter/setter) on a
    POU or interface (COM).

    Unlike plc_create (which makes top-level POU/DUT/GVL objects in a folder),
    these are created on the POU node itself. Whether the parent is an
    interface is detected at runtime and the matching subType is chosen
    automatically (interfaces use a separate set: method 610, property 612).

    Member types:
      method     — callable method (POU 609 / interface 610)
      property   — property; TwinCAT usually adds Get/Set automatically
                   (POU 611 / interface 612). Write accessors via plc_write
                   with method_name="<Prop>.Get" / "<Prop>.Set".
      action     — action, POU only (608)
      transition — SFC transition, POU only (616)
      propget / propset — add a single accessor to an existing property
                   (POU 613/614, interface 654/655)

    Args:
        pou: parent POU or interface name, e.g. "FB_Motor" / "I_Axis".
        name: member name, e.g. "Start" or "Position".
        member_type: method | property | action | transition | propget | propset.
        return_type: return type for property/propget/propset (default "BOOL").
        language: IEC language for method/action/transition (default "ST").
        declaration: optional initial declaration text.
        implementation: optional initial implementation text.

    Returns {pou, member, type, parentKind, status:"created"}.
    """
    valid = {"method", "property", "action", "transition", "propget", "propset"}
    if member_type not in valid:
        raise ValueError(f"member_type must be one of {sorted(valid)}")
    return ps.com_new_member(pou, name, member_type=member_type,
                             return_type=return_type, language=language,
                             declaration=declaration, implementation=implementation)


@mcp.tool()
def plc_delete_member(pou: str, name: str, member_type: str = "method",
                      path: str = "", dry_run: bool = False,
                      force: bool = False) -> dict:
    """Delete one member inside a POU or interface via COM DeleteChild.

    For ``propget``/``propset``, ``name`` is the property name and the tool
    deletes its ``Get``/``Set`` accessor. This is destructive.

    Args:
        pou: parent POU or interface name.
        name: member name, or property name for propget/propset.
        member_type: method | property | action | transition | propget | propset.
        path: optional exact parent tree path returned by plc_find.
    """
    return ps.com_delete_member(pou, name, member_type=member_type, path=path,
                                dry_run=dry_run, force=force)


@mcp.tool()
def plc_rename_member(pou: str, old_name: str, new_name: str,
                      path: str = "") -> dict:
    """Rename one method/property/action/transition inside a POU or interface."""
    return ps.com_rename_member(pou, old_name, new_name, path=path)


@mcp.tool()
def plc_create_fb(name: str, author: str = "", purpose: str = "",
                  with_enum: bool = True) -> dict:
    """Create a STANDARD device-FB conforming to docs/plc_coding_standard.md.

    Emits the documented skeleton — Chinese header comment, standard status quad
    (bDone/bBusy/bError/nErrId), and a CASE eState state machine — plus the
    companion ``E_<Name>State`` enum (Idle/Running/Done/Error, qualified_only).
    Use this instead of plc_create for new device FBs so they are standard-
    compliant and compile clean out of the box.

    Args:
        name: FB name (FB_ prefix auto-added if missing).
        author: Author, filled into the header comment.
        purpose: One-line responsibility, filled into the header comment.
        with_enum: Also create the companion E_<Name>State enum (default True;
            the FB declaration references it, so keep True unless it exists).

    Returns {status, fb, enum?}.
    """
    from .fb_scaffold import standard_fb
    sk = standard_fb(name, author=author, purpose=purpose)
    out: dict[str, Any] = {"status": "created", "fb": sk["fb_name"]}
    if with_enum:
        ps.com_new_pou(sk["enum_name"], pou_type="enum",
                       declaration=sk["enum_declaration"], implementation="")
        out["enum"] = sk["enum_name"]
    ps.com_new_pou(sk["fb_name"], pou_type="fb",
                   declaration=sk["fb_declaration"],
                   implementation=sk["fb_implementation"])
    return out


@mcp.tool()
def plc_create_project(name: str = "PLC1",
                       template: str = "Standard PLC Template") -> dict:
    """Create a new PLC project INSIDE the currently open solution (COM).

    Adds a PLC project node under TIPC of the open solution. Use this when a
    solution is already open in XAE but has no PLC project yet — unlike
    plc_create, which only adds POU/DUT/GVL objects inside an existing project.

    To create a whole new solution from scratch (motion/PackML/etc.), use
    template_create instead.

    Args:
        name: PLC project name (default "PLC1").
        template: PLC project template — "Standard PLC Template" (default:
            MAIN + PlcTask + standard library refs) or "Empty PLC Template"
            (bare, no MAIN).

    Returns {name, template, status:"created"}. Errors if a PLC project with
    the same name already exists in the solution.
    """
    return ps.com_create_plc_project(name, template)


@mcp.tool()
def plc_remove_project(name: str) -> dict:
    """Remove a PLC project from the open solution but retain its disk files.

    Removes only the TIPC node. Returns {name, status:"removed",
    files_deleted:false}. Use plc_delete_project when the local PLC project
    directory must also be deleted.
    """
    return ps.com_remove_plc_project(name)


@mcp.tool()
def plc_delete_project(name: str) -> dict:
    """Delete a PLC project from XAE and delete its local project directory.

    The path is read from the saved .tsproj PrjFilePath metadata and rejected
    unless it is a dedicated child directory containing no other .plcproj.
    Returns the exact deleted_directory. This operation is irreversible.
    """
    return ps.com_delete_plc_project(name)


@mcp.tool()
def template_create(template: str, name: str, output_dir: str,
                    plc_name: str | None = None,
                    tc_version: str | None = None) -> dict:
    """Scaffold a BRAND-NEW TwinCAT solution (.sln + .tsproj) from a template.

    Copies one of the 14 library templates to disk with fresh GUIDs and the
    given name substituted. Filesystem-only — does NOT require or open XAE;
    returns the .sln path so the solution can be opened afterward.

    Template library (call template_list for descriptions/variables):
      Motion / servo :  model1, spt-axishoming, spt-camming, spt-vm-axis,
                        spt-multimaster, spt-dynamic-axis, spt-ax5000-soe-reset
      Machine/PackML :  packml, spt-simplemachine, spt-modemanager,
                        spt-controlledstop, spt-externalsequence
      Alarms         :  spt-alarms, spt-mutingalarms

    Args:
        template: Template name from the library above.
        name: New project / solution name (substituted for PROJECT_NAME).
        output_dir: Parent directory; a subfolder named ``name`` is created.
        plc_name: PLC project name inside the solution (default: template's own).
        tc_version: TwinCAT version string (e.g. "3.1.4024.0"). Auto-detected
            from the local install when omitted.

    Returns {output_dir, files_created, guid_count, sln_path, timestamp}.
    """
    from pathlib import Path as _Path
    from .scaffold import scaffold

    out = str(_Path(output_dir) / name)
    uvars: dict[str, str] = {"PROJECT_NAME": name}
    if plc_name:
        uvars["PLC_NAME"] = plc_name
    if tc_version:
        uvars["TC_VERSION"] = tc_version
    else:
        try:
            from .tc_platform import get_local_tc_version
            v = get_local_tc_version().get("version_str")
            if v:
                uvars["TC_VERSION"] = v
        except Exception:
            pass

    r = scaffold(template_names=[template], output_dir=out, user_vars=uvars,
                 interactive=False, dry_run=False, no_hooks=True)
    slns = list(_Path(r["output_dir"]).glob("*.sln"))
    r["sln_path"] = str(slns[0]) if slns else ""
    return r


@mcp.tool()
def tc_solution_templates() -> list:
    """List locally available templates for an empty XAE solution."""
    from .solution_creation import template_catalog
    return template_catalog()


@mcp.tool()
def tc_create_solution(name: str, output_dir: str, template: str) -> dict:
    """Create and open a new template solution in the selected empty XAE; no runtime actions."""
    from .solution_creation import create_solution
    return create_solution(name, output_dir, template)


@mcp.tool()
def plc_lint(check_build: bool = True) -> dict:
    """Lint the open PLC project against docs/plc_coding_standard.md.

    Reads every POU's declaration/implementation in one COM pass and applies
    the coding-standard §7 checklist as regex rules (all warning-level):
      naming-object · fb-header · fb-status-quad · naming-var · magic-errid ·
      tab-indent. Optionally also runs plc_build for the mandatory 0-error gate.

    Args:
        check_build: Also compile and report errorCount (default True).

    Returns {summary:{total, by_rule}, findings:[{rule, severity, object,
             area, message}], build?:{errorCount, failedProjects}}.
    """
    from .lint import lint_objects, summarize
    findings = lint_objects(ps.com_all_code())
    out: dict[str, Any] = {"summary": summarize(findings), "findings": findings}
    if check_build:
        b = ps.com_build()
        out["build"] = {
            "errorCount": b.get("errorCount", 0),
            "failedProjects": b.get("failedProjects", 0),
            "errors": b.get("errors", []),
            "warnings": b.get("warnings", []),
            "errorsRead": b.get("errorsRead", False),
            "errorSource": b.get("errorSource", ""),
            "diagnosticsPending": b.get("diagnosticsPending", False),
            "message": b.get("message", ""),
        }
    return out


@mcp.tool()
def plc_static_analysis(max_complexity: int = 20, rule_severities: dict | None = None) -> dict:
    """Run offline TCSA checks against every PLC object in the open XAE project.

    This is a no-write, no-build analysis based on declaration and ST source
    text. It does not require TE1200, but the returned result explicitly says
    that TE1200 was not executed and cannot be used as a TE1200 pass result.

    Args:
        max_complexity: Warn when one implementation/method exceeds this
            ST subset cognitive-complexity score (default 20).
        rule_severities: Optional user-selected SAxxxx: off/warning/error mapping.
    """
    from .static_analysis import analyze_objects
    return analyze_objects(ps.com_all_code(), max_complexity=max_complexity,
                           rule_severities=rule_severities)


@mcp.tool()
def plc_build(always_read_errors: bool = True) -> dict:
    """Build the solution and return compile errors.

    The default reads the refreshed Error List so a failed PLC build cannot be
    reported as errorCount=0 merely because diagnostics were not fetched.
    Set always_read_errors=False only for a lightweight status-only check.

    Args:
        always_read_errors: force reading the list even on success (needed to
            see warnings; costs a brief focus steal).

    Returns {failedProjects (0 = success), errorCount, errors[], warnings[],
             errorsRead}.
    """
    return ps.com_build(always_read_errors=always_read_errors)


@mcp.tool()
def plc_delete(name: str, path: str = "", dry_run: bool = False,
               force: bool = False) -> dict:
    """Delete a PLC object (POU/DUT/GVL/Interface/VISU) by name via COM DeleteChild.

    Destructive. Returns {name, folder, status:"deleted"}.
    """
    return ps.com_delete_pou(name, path=path, dry_run=dry_run, force=force)


@mcp.tool()
def plc_rename(old_name: str, new_name: str, path: str = "") -> dict:
    """Rename a PLC object via COM. Fails if new_name already exists in that folder.

    Returns {old, new, folder, status:"renamed"}.
    """
    return ps.com_rename(old_name, new_name, path=path)


@mcp.tool()
def plc_restore_snapshot(snapshot: str = "latest", apply: bool = False) -> dict:
    """Preview or transactionally restore a project-local PLC code snapshot.

    With apply=False this is read-only preview. With apply=True it creates a
    backup, restores code, builds, and automatically rolls back on failure.
    Structural object/member differences block the operation.
    """
    from tc_agent.plc_versions import restore_snapshot
    return restore_snapshot(snapshot, apply=apply)


@mcp.tool()
def plc_structure() -> dict:
    """Return the PLC project structure tree via COM.

    Returns:
        {project, folders: {POUs:[{name, methods[]}], DUTs:[...], GVLs:[...], ...}}.
    """
    return ps.com_structure()


@mcp.tool()
def plc_vars(pou: str | None = None) -> list[dict]:
    """List declared variables across the project (or one POU) via COM.

    Parses VAR blocks of every POU/GVL declaration. DUT members are excluded
    (they are type fields, not runtime variables).

    Args:
        pou: optional object name to restrict to.
    Returns:
        list of {pou, name, type, scope}, sorted by (pou, scope, name).
    """
    return ps.list_variables(pou)


@mcp.tool()
def plc_search(pattern: str, regex: bool = False, ignore_case: bool = True) -> list[dict]:
    """Search all object declarations, implementations, and method bodies via COM.

    Args:
        pattern: substring (default) or regular expression (regex=True).
        regex: treat pattern as a regex.
        ignore_case: case-insensitive (default True).
    Returns:
        list of {pou, area, line, text} matches. Method hits use "POU.method" as pou.
    """
    return ps.search_code(pattern, regex=regex, ignore_case=ignore_case)


# =====================================================================
#  Runtime control — routed through PowerShell. Require running XAE.
#  READ-ONLY: tc_state. DISRUPTIVE (stop the running PLC / restart the
#  runtime): tc_activate, tc_restart, tc_login/logout/start/stop, tc_online,
#  tc_config_mode, tc_run_mode.
# =====================================================================

@mcp.tool()
def tc_state() -> dict:
    """Read the TwinCAT runtime state (read-only).

    Returns:
        {state: NotStarted|Config|Run|Started, started: bool,
         twincatState, runMode}.
    """
    return ps.com_state()


@mcp.tool()
def tc_activate() -> dict:
    """Activate the configuration to the target (writes registry only).

    DISRUPTIVE. Must be followed by tc_restart() to actually load the runtime.
    It does not switch the target to Config mode.
    """
    return ps.com_activate()


@mcp.tool()
def tc_platform_list() -> dict:
    """List exact solution configuration/platform full values; read-only."""
    return ps.com_list_build_platforms()


@mcp.tool()
def tc_platform_show() -> dict:
    """Read current XAE solution platform and project mappings."""
    return ps.com_get_build_platform()


@mcp.tool()
def tc_platform_set(full: str = '', apply: bool = False, acknowledge_target_platform: bool = False,
                    allow_configuration_change: bool = False) -> dict:
    """Preview/select a listed full platform and verify readback. No build/login/deploy."""
    return ps.com_select_build_platform(full, apply, acknowledge_target_platform, allow_configuration_change)


@mcp.tool()
def tc_restart() -> dict:
    """Start/restart the TwinCAT runtime, polling until started.

    DISRUPTIVE: stops any running PLC and reloads the activated configuration.
    """
    return ps.com_restart()


@mcp.tool()
def tc_login(runtime: str = '', all_plcs: bool = False) -> dict:
    """Log in to the PLC runtime (online). DISRUPTIVE."""
    return ps.com_login(runtime, all_plcs)


@mcp.tool()
def tc_logout(runtime: str = '', all_plcs: bool = False) -> dict:
    """Log out from the PLC runtime. DISRUPTIVE."""
    return ps.com_logout(runtime, all_plcs)


@mcp.tool()
def tc_start(runtime: str = '', all_plcs: bool = False) -> dict:
    """Start the PLC program. DISRUPTIVE."""
    return ps.com_start(runtime, all_plcs)


@mcp.tool()
def tc_stop(runtime: str = '', all_plcs: bool = False) -> dict:
    """Stop the PLC program. DISRUPTIVE."""
    return ps.com_stop(runtime, all_plcs)


@mcp.tool()
def tc_online(runtime: str = '', all_plcs: bool = False) -> dict:
    """Verify login/ProgramLoaded, then start and verify selected ADS ports. DISRUPTIVE.

    Use after tc_build for a code-only change (no I/O remap needed).
    """
    return ps.com_online(runtime, all_plcs)


@mcp.tool()
def tc_config_mode() -> dict:
    """Explicitly switch the target runtime to Config mode.

    DISRUPTIVE (stops the PLC).  It is not a deployment prerequisite; use it
    only when the user requests it or before an explicitly requested hardware
    scan that requires Config mode.
    """
    return ps.com_config_mode()


@mcp.tool()
def tc_run_mode() -> dict:
    """Switch the target runtime to Run mode. DISRUPTIVE. Requires activated config."""
    return ps.com_run_mode()


@mcp.tool()
def tc_system_structure(max_depth: int = 6, roots: list[str] | None = None) -> dict:
    """Read the live XAE SYSTEM tree (TIRC/TIRS/TIRT) without changing it."""
    return ps.com_system_structure(max_depth=max_depth, roots=roots)


@mcp.tool()
def tc_system_settings() -> dict:
    """Read SYSTEM > Real-Time > Settings, CPU selection and task attributes."""
    return ps.com_system_settings()


@mcp.tool()
def tc_system_settings_set(settings: dict, apply: bool = False) -> dict:
    """Preview or write allow-listed TIRS settings; apply=True is required to mutate."""
    return ps.com_system_settings_set(settings, apply=apply)


@mcp.tool()
def tc_core_info() -> dict:
    """Read TwinCAT CPU/core selection and affinity settings."""
    return ps.com_core_info()


@mcp.tool()
def tc_realtime_info() -> dict:
    """Read normalized 4024/4026 memory, core and task real-time settings."""
    return ps.com_realtime_info()


@mcp.tool()
def tc_realtime_validate() -> dict:
    """Read and validate RT memory, cores, priorities and task/BaseTime compatibility."""
    return ps.com_realtime_validate()


@mcp.tool()
def tc_hmi_project_info(project: str = "") -> dict:
    """Read the open TwinCAT HMI project's version, startup view and project metadata."""
    return ps.com_hmi_project_info(project)


@mcp.tool()
def tc_hmi_create_project(name: str, output_directory: str = "", template: str = "",
                          apply: bool = False) -> dict:
    """Preview/create an HMI project in XAE from the installed official TE2000 template."""
    return ps.com_hmi_create_project(name, output_directory=output_directory,
                                     template=template, apply=apply)


@mcp.tool()
def tc_hmi_structure(project: str = "") -> dict:
    """Inventory HMI views, contents, scripts, themes, assets and server configuration files."""
    return ps.com_hmi_structure(project)


@mcp.tool()
def tc_hmi_control_schema(project: str = "", control_type: str = "", attribute: str = "") -> dict:
    """Read exact installed HMI control types, inherited attributes and value schemas before writing."""
    from .hmi_contract import control_schema
    return control_schema(project, control_type, attribute)


@mcp.tool()
def tc_hmi_source_index(project: str = "", refresh: bool = False) -> dict:
    """Incrementally index saved HMI project references; never save XAE or read PLC runtime."""
    from .hmi_source import source_index
    return source_index(project, refresh)


@mcp.tool()
def tc_hmi_source_catalog(project: str = "", kind: str = "files", file: str = "",
                          query: str = "", offset: int = 0, limit: int = 80) -> dict:
    """Page the saved HMI file/control index; continue using next_offset."""
    from .hmi_source import source_catalog
    return source_catalog(project, kind=kind, file=file, query=query, offset=offset, limit=limit)


@mcp.tool()
def tc_hmi_read_smart(file: str, project: str = "", area: str = "auto", control_id: str = "",
                       include_content: bool | None = None, control_offset: int = 0,
                       content_offset: int = 0, max_controls: int = 40,
                       max_chars: int = 12000, refresh: bool = False) -> dict:
    """Prefer indexed saved HMI source, exact controls/events/bindings. Dirty editor state is unknown.

    Auto returns controls for markup, source for scripts/config. Follow returned
    next_control_offset/next_content_offset (UTF-16); do not guess offsets.
    """
    from .hmi_source import read_smart
    return read_smart(file, project, area=area, control_id=control_id,
                      include_content=include_content, control_offset=control_offset,
                      content_offset=content_offset, max_controls=max_controls,
                      max_chars=max_chars, refresh=refresh)


@mcp.tool()
def tc_hmi_read(file: str, project: str = "", max_chars: int = 200000,
                control_id: str = "", include_content: bool = True,
                max_controls: int = 200, control_offset: int = 0, content_offset: int = 0) -> dict:
    """Read saved HMI source/controls with exact-ID filtering or returned next offsets (UTF-16 for source)."""
    return ps.com_hmi_read(
        file, project=project, max_chars=max_chars, control_id=control_id,
        include_content=include_content, max_controls=max_controls,
        control_offset=control_offset, content_offset=content_offset,
    )


@mcp.tool()
def tc_hmi_write_markup(file: str, markup: str, project: str = "", apply: bool = False) -> dict:
    """Preview/replace one existing HMI markup file through XAE DTE with backup and readback."""
    return ps.com_hmi_write_markup(file, markup, project=project, apply=apply)


@mcp.tool()
def tc_hmi_ads_info(project: str = "") -> dict:
    """Read default/remote ADS runtime definitions without connecting to a PLC."""
    return ps.com_hmi_ads_info(project)


@mcp.tool()
def tc_hmi_validate(project: str = "") -> dict:
    """Structurally validate HMI markup, startup view and ADS JSON without running the client."""
    return ps.com_hmi_validate(project)


@mcp.tool()
def tc_hmi_create_view(name: str, project: str = "", kind: str = "view",
                       controls: list[dict] | None = None,
                       apply: bool = False) -> dict:
    """Preview/create a .view or .content through official AddView/AddContent
    and semantic AddControl/ChangeAttributes; no project reload is performed.
    """
    return ps.com_hmi_create_view(
        name, project=project, kind=kind, controls=controls, apply=apply,
    )


@mcp.tool()
def tc_hmi_project_api(operation: str, project: str = "",
                       arguments: dict | None = None, apply: bool = False) -> dict:
    """Call an official ITcHmiProject member through a preview/apply contract.

    Use operation='catalog' first. Mutating members remain preview-only until
    apply=true and return an official API/readback summary.
    """
    return ps.com_hmi_project_api(operation, project=project,
                                  arguments=arguments, apply=apply)


@mcp.tool()
def tc_hmi_startup_view_set(view: str, project: str = "") -> dict:
    """Set the HMI startup View through ITcHmiProject.ChangeStartupView without project reload."""
    return ps.com_hmi_startup_view_set(view, project=project)


@mcp.tool()
def tc_hmi_control_edit(file: str, action: str, control_id: str, project: str = "",
                        control_type: str = "", parent_id: str = "",
                        attributes: dict | None = None, apply: bool = False) -> dict:
    """Preview/add/update/remove one HMI control via DTE TextDocument; verify without project reload."""
    return ps.com_hmi_control_edit(
        file, action, control_id, project=project, control_type=control_type,
        parent_id=parent_id, attributes=attributes, apply=apply,
    )


@mcp.tool()
def tc_hmi_controls_batch(file: str, operations: list[dict], project: str = '', apply: bool = False) -> dict:
    """Prefer for multiple controls on one page: 1..100 ordered add/update/remove operations,
    unique control_id, parent first. Installed schema validation, one DTE save/readback,
    no project reload. Preview by default; does not verify browser or live ADS.
    """
    from .hmi_batch import controls_batch
    return controls_batch(file, operations, project, apply)


@mcp.tool()
def tc_hmi_control_events(file: str, control_id: str, project: str = '', event: str = '',
                          actions: list[dict] | None = None, action: str = 'read',
                          apply: bool = False, placement: str = 'native') -> dict:
    """Inspect supported events or preview/edit Trigger actions without project reload."""
    from .hmi_events import control_events
    return control_events(file, control_id, project, event, actions, action, apply, placement)


@mcp.tool()
def tc_hmi_delete_view(file: str, project: str = "", apply: bool = False) -> dict:
    """Preview/delete a non-startup HMI view/content with a recoverable local backup."""
    return ps.com_hmi_delete_view(file, project=project, apply=apply)


@mcp.tool()
def tc_hmi_item_delete(file: str, project: str = '', apply: bool = False, repair_orphan: bool = False) -> dict:
    """Preview/native-delete an HMI item with reference gates, backup and config cleanup."""
    from .hmi_delete import delete_item
    return delete_item(file, project, apply, repair_orphan)


@mcp.tool()
def tc_hmi_ads_runtime_set(name: str, project: str = "", scope: str = "both",
                           action: str = "upsert", settings: dict | None = None,
                           apply: bool = False) -> dict:
    """Preview/upsert/remove one saved HMI ADS Runtime and verify JSON readback."""
    return ps.com_hmi_ads_runtime_set(
        name, project=project, scope=scope, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_ads_symbols(project: str = "", runtime: str = "", scope: str = "") -> dict:
    """Read saved HMI ADS symbol mappings without connecting to the PLC."""
    return ps.com_hmi_ads_symbols(project=project, runtime=runtime, scope=scope)


@mcp.tool()
def tc_hmi_ads_symbol_set(runtime: str, name: str, project: str = "", scope: str = "both",
                          action: str = "upsert", index_group: int = 0,
                          index_offset: int = 0, type_name: str = "",
                          apply: bool = False) -> dict:
    """Preview/upsert/remove one ADS mapped symbol using the installed HMI server schema."""
    return ps.com_hmi_ads_symbol_set(
        runtime, name, project=project, scope=scope, action=action,
        index_group=index_group, index_offset=index_offset, type_name=type_name, apply=apply,
    )


@mcp.tool()
def tc_hmi_bindings(project: str = "") -> dict:
    """Statically inventory HMI SymbolExpressions and check saved runtime/mapping/internal/control references."""
    return ps.com_hmi_bindings(project)


@mcp.tool()
def tc_hmi_dynamic_symbols_set(symbols: dict, definitions: dict | None = None,
                               project: str = "", apply: bool = False) -> dict:
    """Preview/register dynamic ADS symbols and schemas in TcHmiSrv, then reload and verify."""
    return ps.com_hmi_dynamic_symbols_set(symbols, definitions=definitions,
                                          project=project, apply=apply)


@mcp.tool()
def tc_hmi_bind_plc(project: str = "", plc: str = "", runtime_name: str = "",
                    symbol_roots: list[str] | None = None,
                    read_only_symbols: list[str] | None = None,
                    scope: str = "default", apply: bool = False) -> dict:
    """Bind selected PLC TMC symbols using the actual XAE target and resolved ADS port."""
    return ps.com_hmi_bind_plc(
        project=project, plc=plc, runtime_name=runtime_name,
        symbol_roots=symbol_roots, read_only_symbols=read_only_symbols,
        scope=scope, apply=apply,
    )


@mcp.tool()
def tc_hmi_internal_symbols(project: str = "") -> dict:
    """Read project internal symbols from Properties/tchmiconfig.json."""
    return ps.com_hmi_internal_symbols(project)


@mcp.tool()
def tc_hmi_internal_symbol_set(name: str, project: str = "", action: str = "upsert",
                               settings: dict | None = None, apply: bool = False) -> dict:
    """Preview/upsert/remove one HMI internal symbol using the project Framework schema."""
    return ps.com_hmi_internal_symbol_set(
        name, project=project, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_localizations(project: str = "") -> dict:
    """Read registered project languages, localization files, keys and missing locale values."""
    return ps.com_hmi_localizations(project)


@mcp.tool()
def tc_hmi_localization_set(key: str, project: str = "", action: str = "upsert",
                            values: dict | None = None, apply: bool = False) -> dict:
    """Preview/upsert/remove one localization key across registered project locales."""
    return ps.com_hmi_localization_set(
        key, project=project, action=action, values=values, apply=apply,
    )


@mcp.tool()
def tc_hmi_themes(project: str = "") -> dict:
    """Read project themes, theme files, active theme and project themed resources."""
    return ps.com_hmi_themes(project)


@mcp.tool()
def tc_hmi_themed_resource_set(name: str, project: str = "", action: str = "upsert",
                               settings: dict | None = None, apply: bool = False) -> dict:
    """Preview/upsert/remove one project themed resource with per-theme values."""
    return ps.com_hmi_themed_resource_set(
        name, project=project, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_active_theme_set(theme: str, project: str = "", apply: bool = False) -> dict:
    """Preview/set the startup active theme and verify it after XAE project reload."""
    return ps.com_hmi_active_theme_set(theme, project=project, apply=apply)


@mcp.tool()
def tc_hmi_user_controls(project: str = "") -> dict:
    """Read registered UserControls, parameter definitions and contained controls."""
    return ps.com_hmi_user_controls(project)


@mcp.tool()
def tc_hmi_user_control_create(name: str, project: str = "",
                               parameters: list[dict] | None = None,
                               controls: list[dict] | None = None,
                               apply: bool = False) -> dict:
    """Preview/create a schema-backed 1.12 UserControl and register both project files."""
    return ps.com_hmi_user_control_create(
        name, project=project, parameters=parameters, controls=controls, apply=apply,
    )


@mcp.tool()
def tc_hmi_user_control_parameter_set(user_control: str, name: str, project: str = "",
                                      action: str = "upsert", settings: dict | None = None,
                                      apply: bool = False) -> dict:
    """Preview/upsert/remove one UserControl parameter using the installed Framework schema."""
    return ps.com_hmi_user_control_parameter_set(
        user_control, name, project=project, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_user_control_delete(user_control: str, project: str = "", apply: bool = False) -> dict:
    """Preview/back up/delete one UserControl and remove both project registrations."""
    return ps.com_hmi_user_control_delete(user_control, project=project, apply=apply)


@mcp.tool()
def tc_hmi_framework_templates(project: str = "") -> dict:
    """List installed TE2000 Framework Project templates and the current HMI target."""
    return ps.com_hmi_framework_templates(project)


@mcp.tool()
def tc_hmi_framework_validate(source: str) -> dict:
    """Structurally validate a .hmiextproj, Manifest and Framework Control resources."""
    return ps.com_hmi_framework_validate(source)


@mcp.tool()
def tc_hmi_framework_control_info(source: str, control: str = "") -> dict:
    """Read one Framework Control description, source path, attributes and events."""
    return ps.com_hmi_framework_control_info(source, control)


@mcp.tool()
def tc_hmi_framework_attribute_set(source: str, name: str, control: str = "",
                                   action: str = "upsert", settings: dict | None = None,
                                   apply: bool = False) -> dict:
    """Preview/upsert/remove a Framework Control attribute and synchronized accessor code."""
    return ps.com_hmi_framework_attribute_set(
        source, name, control=control, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_framework_event_set(source: str, name: str, control: str = "",
                               action: str = "upsert", settings: dict | None = None,
                               apply: bool = False) -> dict:
    """Preview/upsert/remove a Framework Control event and synchronized raise helper."""
    return ps.com_hmi_framework_event_set(
        source, name, control=control, action=action, settings=settings, apply=apply,
    )


@mcp.tool()
def tc_hmi_framework_create(name: str, output_directory: str, project: str = "",
                            language: str = "typescript", description: str = "",
                            apply: bool = False) -> dict:
    """Preview/create a native1.12 Framework Control project from installed TE2000 templates."""
    return ps.com_hmi_framework_create(
        name, output_directory, project=project, language=language,
        description=description, apply=apply,
    )


@mcp.tool()
def tc_hmi_framework_pack(source: str, output_directory: str = "",
                          version: str = "", apply: bool = False) -> dict:
    """Preview or create a locally verified Framework Control NuGet package; do not install it."""
    return ps.com_hmi_framework_pack(
        source, output_directory=output_directory, version=version, apply=apply,
    )


@mcp.tool()
def tc_hmi_framework_packages(project: str = "", package_id: str = "") -> dict:
    """List actual installed versions and existing package_path; optionally filter by exact ID. Never guess paths."""
    return ps.com_hmi_framework_packages(project, package_id)


@mcp.tool()
def tc_hmi_framework_package_inspect(package: str) -> dict:
    """Inspect an actual package_path from inventory: controls, functions, framework or resources; no install."""
    return ps.com_hmi_framework_package_inspect(package)


@mcp.tool()
def tc_hmi_framework_install(package: str, project: str = "", apply: bool = False,
                             acknowledge_package_change: bool = False) -> dict:
    """Preview/install a Framework Control package transactionally; apply requires hard acknowledgement."""
    return ps.com_hmi_framework_install(
        package, project=project, apply=apply,
        acknowledge_package_change=acknowledge_package_change,
    )


@mcp.tool()
def tc_hmi_framework_uninstall(package_id: str, project: str = "", force: bool = False,
                               apply: bool = False,
                               acknowledge_package_change: bool = False) -> dict:
    """Preview/uninstall a non-core Framework package with markup reference gate and rollback."""
    return ps.com_hmi_framework_uninstall(
        package_id, project=project, force=force, apply=apply,
        acknowledge_package_change=acknowledge_package_change,
    )


@mcp.tool()
def tc_hmi_runtime_info(project: str = "") -> dict:
    """Discover the active HMI Engineering Server and resolve the real application entry URL."""
    return ps.com_hmi_runtime_info(project)


@mcp.tool()
def tc_hmi_server_control(action: str, project: str = "", apply: bool = False) -> dict:
    """Preview/start/stop/restart the exact project's hidden HMI Engineering Server."""
    return ps.com_hmi_server_control(action, project=project, apply=apply)


@mcp.tool()
def tc_hmi_ads_live_check(project: str = "", runtime: str = "", plc: str = "",
                          symbols: list[str] | None = None, max_depth: int = 3,
                          max_symbols: int = 32) -> dict:
    """Read-only verification of configured HMI mappings against the actual PLC ADS endpoint."""
    return ps.com_hmi_ads_live_check(
        project=project, runtime=runtime, plc=plc, symbols=symbols,
        max_depth=max_depth, max_symbols=max_symbols,
    )


@mcp.tool()
def tc_hmi_binding_diagnose(project: str = "", runtime: str = "", plc: str = "",
                            max_symbols: int = 32) -> dict:
    """Diagnose expressions, TMC export, HMI mapping, endpoint and live ADS in one read-only workflow."""
    return ps.com_hmi_binding_diagnose(
        project=project, runtime=runtime, plc=plc, max_symbols=max_symbols,
    )


@mcp.tool()
def tc_hmi_browser_validate(project: str = "", widths: list[int] | None = None,
                            height: int = 720, settle_ms: int = 5000,
                            entry_page: str = "") -> dict:
    """Validate the running HMI in a hidden browser and collect runtime/network/layout diagnostics."""
    return ps.com_hmi_browser_validate(
        project=project, widths=widths, height=height, settle_ms=settle_ms,
        entry_page=entry_page,
    )


@mcp.tool()
def tc_hmi_build(project: str = "") -> dict:
    """Build one HMI project through DTE without focusing or copying the Error List."""
    return ps.com_hmi_build(project)


@mcp.tool()
def tc_hmi_diagnostics(project: str = "", check_page: bool = True) -> dict:
    """Read XAE diagnostics, historical HMI Server events and existing HTTP entry; never build/start/write PLC."""
    from .hmi_diagnostics import read_hmi_diagnostics
    return read_hmi_diagnostics(project, check_page)


@mcp.tool()
def tc_safety_structure(max_depth: int = 8) -> dict:
    """Read the TwinSAFE TISC project tree without changing it."""
    return ps.com_safety_structure(max_depth)


@mcp.tool()
def tc_safety_project_info(project: str = "") -> dict:
    """Read Safety project metadata exposed by Automation Interface."""
    return ps.com_safety_project_info(project)


@mcp.tool()
def tc_safety_files(project: str) -> dict:
    """Inventory and classify files in a TISC or filesystem Safety project."""
    return ps.com_safety_files(project)


@mcp.tool()
def tc_safety_target_info(project: str) -> dict:
    """Read Safety project and TargetSystemConfig metadata without hardware validation."""
    return ps.com_safety_target_info(project)


@mcp.tool()
def tc_safety_aliases(project: str, group: str = "") -> dict:
    """Read SDS Alias Devices, channels and stored mapping fields."""
    return ps.com_safety_aliases(project, group=group)


@mcp.tool()
def tc_safety_application(project: str, group: str = "") -> dict:
    """Read SAL/Safety C structure, including graphical FB ports and stored wiring."""
    return ps.com_safety_application(project, group=group)


@mcp.tool()
def tc_safety_logic_check(project: str, group: str = "") -> dict:
    """Check stored TwinSAFE FB, port, wire and Alias Device reference consistency."""
    return ps.com_safety_logic_check(project, group=group)


@mcp.tool()
def tc_safety_validate(source: str = "") -> dict:
    """Structurally validate TISC and optionally a .splcproj/.tfzip source."""
    return ps.com_safety_validate(source)


@mcp.tool()
def tc_safety_import(source: str, name: str = "", mode: str = "copy",
                     apply: bool = False, confirm_source_move: bool = False,
                     acknowledge_safety_review: bool = False) -> dict:
    """Preview/import a Safety template; never downloads or activates it."""
    return ps.com_safety_import(
        source, name=name, mode=mode, apply=apply,
        confirm_source_move=confirm_source_move,
        acknowledge_safety_review=acknowledge_safety_review,
    )


@mcp.tool()
def tc_safety_create(name: str, target: str = "hardware",
                     template: str = "preconfigured-inputs",
                     author: str = "TwinCAT Agent", internal_project_name: str = "",
                     apply: bool = False,
                     acknowledge_safety_review: bool = False) -> dict:
    """Create from an installed Beckhoff Safety template; preconfigured inputs is default."""
    return ps.com_safety_create(
        name, target=target, template=template, author=author,
        internal_project_name=internal_project_name, apply=apply,
        acknowledge_safety_review=acknowledge_safety_review,
    )


@mcp.tool()
def tc_safety_export(project: str, output_file: str, overwrite: bool = False,
                     apply: bool = False,
                     acknowledge_safety_review: bool = False) -> dict:
    """Preview/archive one Safety project directory to a verified .tfzip container."""
    return ps.com_safety_export(
        project, output_file, overwrite=overwrite, apply=apply,
        acknowledge_safety_review=acknowledge_safety_review,
    )


@mcp.tool()
def tc_safety_remove(project: str, apply: bool = False,
                     confirm_project_name: str = "",
                     acknowledge_safety_review: bool = False) -> dict:
    """Explicitly remove from TISC while preserving files; use delete for normal deletion."""
    return ps.com_safety_remove(
        project, apply=apply, confirm_project_name=confirm_project_name,
        acknowledge_safety_review=acknowledge_safety_review,
    )


@mcp.tool()
def tc_safety_delete(project: str, backup_file: str = "", apply: bool = False,
                     confirm_project_name: str = "", confirm_delete_files: bool = False,
                     acknowledge_safety_review: bool = False) -> dict:
    """Default Safety deletion: back up and delete TISC plus files, including orphans."""
    return ps.com_safety_delete(
        project, backup_file=backup_file, apply=apply,
        confirm_project_name=confirm_project_name,
        confirm_delete_files=confirm_delete_files,
        acknowledge_safety_review=acknowledge_safety_review,
    )


@mcp.tool()
def tc_realtime_settings_set(settings: dict, apply: bool = False) -> dict:
    """Preview/write global and per-core RT settings with 4024/4026 gates."""
    return ps.com_system_settings_set(settings, apply=apply)


@mcp.tool()
def tc_core_assign(cpu_ids: list[int], max_cpus: int | None = None,
                   affinity: int | None = None,
                   p_core_affinity: int | None = None,
                   e_core_affinity: int | None = None,
                   apply: bool = False) -> dict:
    """Preview or assign TwinCAT CPU cores; apply=True writes and verifies TIRS."""
    return ps.com_core_assign(
        cpu_ids, max_cpus=max_cpus, affinity=affinity,
        p_core_affinity=p_core_affinity,
        e_core_affinity=e_core_affinity, apply=apply,
    )


@mcp.tool()
def tc_task_info(max_depth: int = 6) -> dict:
    """Read the live TIRT real-time task tree and exposed XML parameters."""
    return ps.com_task_info(max_depth=max_depth)


@mcp.tool()
def tc_task_core_assign(task_path: str, cpu_id: int, apply: bool = False) -> dict:
    """Preview or assign a TIRT task core when XAE exposes a writable core field."""
    return ps.com_task_core_assign(task_path, cpu_id, apply=apply)


@mcp.tool()
def tc_task_settings_set(task_path: str, settings: dict,
                         apply: bool = False) -> dict:
    """Preview/write task priority, cycle and watchdog fields with RT validation."""
    return ps.com_task_settings_set(task_path, settings, apply=apply)


@mcp.tool()
def tc_system_add(parent_path: str, name: str, item_type: int,
                  info: str = "", apply: bool = False) -> dict:
    """Preview or CreateChild a SYSTEM node under TIRC/TIRT; apply=True mutates."""
    return ps.com_system_add(parent_path, name, item_type, info=info, apply=apply)


@mcp.tool()
def tc_system_remove(path: str, apply: bool = False,
                     allow_with_children: bool = False) -> dict:
    """Preview or delete one exact TIRC/TIRT node; roots cannot be deleted."""
    return ps.com_system_remove(path, apply=apply, allow_with_children=allow_with_children)


@mcp.tool()
def tc_realtime_refresh() -> dict:
    """Refresh the currently open XAE SYSTEM > Real-Time page only."""
    return ps.com_realtime_refresh()


# =====================================================================
#  Template tools — pure Python, no XAE required.
# =====================================================================

def _meta_brief(m: Any) -> dict:
    return {
        "name": m.name,
        "display_name": getattr(m, "display_name", "") or "",
        "description": getattr(m, "description", "") or "",
        "category": getattr(m, "category", "") or "",
        "tags": list(getattr(m, "tags", []) or []),
    }


@mcp.tool()
def template_list(category: str | None = None, tag: str | None = None) -> list[dict]:
    """List available PLC project templates, optionally filtered by category/tag.

    Each item: {name, display_name, description, category, tags}.
    """
    metas = repository.list_templates(category=category, tag=tag)
    return [_meta_brief(m) for m in metas]


@mcp.tool()
def template_info(name: str) -> dict:
    """Show a single template's full metadata (variables, GUID strategy, etc.).

    Args:
        name: template name, e.g. "packml" or "spt-axishoming".
    """
    m = repository.get_template(name)
    return m.to_dict() if hasattr(m, "to_dict") else _meta_brief(m)


@mcp.tool()
def template_validate(name: str) -> dict:
    """Validate a template's integrity. Returns {name, valid, error?}."""
    try:
        repository.validate_template(name)
        return {"name": name, "valid": True}
    except Exception as e:  # TemplateValidationError / NotFound
        return {"name": name, "valid": False, "error": str(e)}


# =====================================================================
#  FB 库 (fblib) — 常用功能块模板：列出/详情/塞入/抽取/校验
# =====================================================================

@mcp.tool()
def fblib_list(category: str | None = None) -> list[dict]:
    """List reusable FB templates in the FB library (fblib/).

    Concrete, tested, standard-compliant function blocks that fblib_add can
    drop into the open project. Optionally filter by category
    (motion | framework | device | communication | tools). Results are
    grouped by category, flagship templates first.

    Each item: {slug, name, category, family, rank, keywords, use_when,
    archetype, description}. family is "spt-framework" (needs the SPT
    libraries, extends FB_PackML_BaseModule / FB_ComponentBase) or
    "standalone" (no framework).
    """
    from .fblib import list_fbs
    return list_fbs(category)


@mcp.tool()
def fblib_find(intent: str) -> dict:
    """Pick the right FB template for what the user wants to build.

    ALWAYS call this before hand-writing PLC code for a device, axis, module
    or protocol. Pass the user's own words ("我要写轴程序", "add a cylinder",
    "搭 SPT 工程骨架"); it routes intent -> category -> ranked templates.

    Returns {intent, matched_categories, results:[{slug, score, why, ...}],
    hint}. If results is non-empty, use fblib_add on results[0].slug rather
    than writing the FB from scratch. If empty, write it by hand and consider
    fblib_extract afterwards to capture it.
    """
    from .fblib import find_fbs
    return find_fbs(intent)


@mcp.tool()
def fblib_info(slug: str) -> dict:
    """Show a FB template's manifest (params, libraries, companions, archetype).

    Args:
        slug: template folder name, e.g. "blink" or "tcpip-client".
    """
    from .fblib import get_fb
    return get_fb(slug)["manifest"]


@mcp.tool()
def fblib_add(slug: str, params: dict | None = None) -> dict:
    """Insert a library FB into the currently open PLC project (COM).

    Creates the FB + companion DUTs (with {{param}} substituted), adds the
    declared TwinCAT library refs, and runs plc_lint as a compliance gate.

    Args:
        slug: FB template folder name.
        params: {PARAM: value} overrides; unset params use manifest defaults.

    Returns {status, fb, companions, libraries, lint:{summary, findings}}.
    """
    from .fblib import add_fb
    return add_fb(slug, params or {})


@mcp.tool()
def fblib_extract(fb_name: str, slug: str, category: str = "",
                  description: str = "", companions: list[str] | None = None) -> dict:
    """Extract an FB from the open project into a new fblib template (COM).

    Reads the FB's declaration/implementation, auto-detects the archetype,
    and writes fblib/<slug>/. libraries/params are left blank for you to fill
    (literals are NOT auto-parameterized — mark {{placeholders}} by hand).

    Args:
        fb_name: existing FB name in the open project.
        slug: new template folder name (kebab-case).
        category: communication | motion | tools | device.
        description: one-line description for the manifest.
        companions: names of companion DUTs to record in the manifest.
    """
    from .fblib import extract_fb
    return extract_fb(fb_name, slug, category=category, description=description,
                      companions=companions)


@mcp.tool()
def fblib_validate(slug: str) -> dict:
    """Lint a FB template offline against the coding standard (no XAE needed).

    Renders the template (default params) and runs the plc lint rules.
    Returns {slug, summary:{total, by_rule}, findings[]}.
    """
    from .fblib import validate_fb
    return validate_fb(slug)


# =====================================================================
#  客户案例库 — 多目录发现、检索、校验、一键导入/更新
# =====================================================================

@mcp.tool()
def case_list(category: str | None = None) -> list[dict]:
    """List built-in and customer-supplied PLCopen case packages.

    Customer cases are discovered recursively from ProgramData/TwinCATAgent/
    CaseLibrary. A higher-version customer package overrides the built-in case
    with the same id. Optionally filter by functional category.
    """
    from .case_library import discover_cases
    items = discover_cases()
    if category:
        items = [item for item in items
                 if item.get("category", "").lower() == category.lower()]
    return items


@mcp.tool()
def case_find(intent: str) -> list[dict]:
    """Find customer examples by function, protocol, device, or keyword."""
    from .case_library import find_cases
    return find_cases(intent)


@mcp.tool()
def case_info(case_id: str) -> dict:
    """Return one case manifest, source path, maturity and validation state."""
    from .case_library import get_case
    return get_case(case_id)


@mcp.tool()
def case_add(case_id: str) -> dict:
    """Import a reusable PLCopen case into the open XAE; skip name conflicts."""
    from .case_library import install_case
    return install_case(case_id, replace=False, build=True)


@mcp.tool()
def case_update(case_id: str) -> dict:
    """Update a reusable PLCopen case in the open XAE; replace name conflicts."""
    from .case_library import install_case
    return install_case(case_id, replace=True, build=True)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
