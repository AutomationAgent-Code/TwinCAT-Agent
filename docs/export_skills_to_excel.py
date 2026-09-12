"""Export TwinCAT Agent skills + commands to Excel."""
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

wb = Workbook()

# --- Styles ---
header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
header_fill = PatternFill(start_color="005099", end_color="005099", fill_type="solid")
sub_header_fill = PatternFill(start_color="0078D4", end_color="0078D4", fill_type="solid")
sub_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
body_font = Font(name="Consolas", size=10)
chinese_font = Font(name="Microsoft YaHei", size=10)
thin_border = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin")
)
wrap = Alignment(wrap_text=True, vertical="top")

# ================================================================
# Sheet 1: Skills Overview
# ================================================================
ws1 = wb.active
ws1.title = "Skills Overview"

skills = [
    ["Skill", "Trigger", "Description", "Key Technology", "Commands"],
    ["TwinCAT Template\nManager", "/twincat-template",
     "Template library management:\ncreate projects from templates,\nextract templates from projects,\nvalidate and remove templates",
     "GUID whitelist scanning,\nXML attribute-level matching,\nplaceholder substitution,\n_Libraries raw copy",
     "7 commands\n(list, info, create,\nadd, inspect, validate,\nremove)"],
    ["PLC Programming", "/plc",
     "PLC code operations:\nread/write POU/DUT/GVL,\nexport/import PLCopen XML,\ncreate objects via COM,\nlive editor editing",
     "COM CreateChild,\nfilesystem XML CDATA,\npyautogui Ctrl+A/C/V,\nmarker-based serialization\n(for CDATA preservation)",
     "12 commands\n(list, read, write, export,\nimport, create-com,\nimport-com, export-com,\nlibraries, reload, build,\nreload)"],
    ["TwinCAT Platform\nControl", "/tc",
     "Runtime control:\nbuild & compile,\nactivate configuration,\nlogin/start/stop PLC,\nRun/Config mode switch,\nfull deploy pipeline",
     "COM ActivateConfiguration,\nConsumeXml online commands,\nSilentMode + pyautogui\ndialog dismissal,\nSolutionBuild.Build()",
     "15 commands\n(build, check, activate,\nboot, login, logout,\nstart, stop, online,\nstate, config, run,\ntarget, silent, deploy)"],
]

for row_idx, row_data in enumerate(skills):
    for col_idx, val in enumerate(row_data):
        cell = ws1.cell(row=row_idx + 1, column=col_idx + 1, value=val)
        cell.border = thin_border
        cell.alignment = wrap
        if row_idx == 0:
            cell.font = header_font
            cell.fill = header_fill
        else:
            cell.font = chinese_font if col_idx in (2, 3) else body_font

# Column widths
ws1.column_dimensions["A"].width = 20
ws1.column_dimensions["B"].width = 22
ws1.column_dimensions["C"].width = 35
ws1.column_dimensions["D"].width = 35
ws1.column_dimensions["E"].width = 20

# ================================================================
# Sheet 2: All Commands
# ================================================================
ws2 = wb.create_sheet("All Commands")

commands = [
    ["Group", "Command", "Usage", "Description", "Layer"],
    # Template
    ["Template", "list", "tc-template list [--category <cat>]", "List all templates with auto-generated descriptions", "Filesystem"],
    ["Template", "info", "tc-template info <name>", "Template metadata, variables, GUID strategy, file tree", "Filesystem"],
    ["Template", "create", "tc-template create <tpl> -n <name> -o <dir>", "Scaffold -> COM open -> read error list", "COM + pyautogui"],
    ["Template", "add", "tc-template add <path> -n <name>", "Extract template from TwinCAT project (GUID protection)", "COM + Filesystem"],
    ["Template", "inspect", "tc-template inspect", "Read error list from open TwinCAT XAE", "COM + pyautogui"],
    ["Template", "validate", "tc-template validate <name>", "Validate template integrity (GUID map, variables)", "Filesystem"],
    ["Template", "remove", "tc-template remove <name>", "Delete a template from repository", "Filesystem"],
    # PLC
    ["PLC", "plc list", "tc-template plc list <dir>", "List all PLC objects by type (FB/DUT/GVL/VISU)", "Filesystem"],
    ["PLC", "plc read", "tc-template plc read <dir> <name>", "Read declaration + implementation + methods", "Filesystem"],
    ["PLC", "plc write", "tc-template plc write <dir> <name> \"code\"", "Write code to file (CDATA preserved)", "Filesystem"],
    ["PLC", "plc export", "tc-template plc export <dir> <name> -o <xml>", "Export POU as PLCopen XML (filesystem)", "Filesystem"],
    ["PLC", "plc import", "tc-template plc import <dir> <xml>", "Import PLCopen XML, create .TcPOU file", "Filesystem"],
    ["PLC", "plc create-com", "tc-template plc create-com <name> -t fb", "Create POU/DUT/GVL via COM Automation Interface", "COM"],
    ["PLC", "plc import-com", "tc-template plc import-com <xml>", "PlcOpenImport via COM", "COM"],
    ["PLC", "plc export-com", "tc-template plc export-com <out> <pous>", "PlcOpenExport via COM", "COM"],
    ["PLC", "plc libraries", "tc-template plc libraries", "List library references (COM)", "COM"],
    ["PLC", "plc reload", "tc-template plc reload", "Trigger IDE File.Reload / Ctrl+S", "pyautogui"],
    ["PLC", "plc build", "tc-template plc build", "Build PLC project + read error list", "COM + pyautogui"],
    # Platform
    ["Platform", "tc build", "tc-template tc build", "Build the PLC project and show errors", "COM"],
    ["Platform", "tc check", "tc-template tc check", "CheckAllObjects() — compile check", "COM"],
    ["Platform", "tc activate", "tc-template tc activate", "ActivateConfig + StartRestartTwinCAT", "COM"],
    ["Platform", "tc boot", "tc-template tc boot", "Set PLC as boot project (autostart)", "COM"],
    ["Platform", "tc login", "tc-template tc login", "Login to PLC runtime (ConsumeXml LoginCmd)", "COM"],
    ["Platform", "tc logout", "tc-template tc logout", "Logout from PLC runtime (ConsumeXml LogoutCmd)", "COM"],
    ["Platform", "tc start", "tc-template tc start", "Start PLC program (ConsumeXml StartCmd)", "COM"],
    ["Platform", "tc stop", "tc-template tc stop", "Stop PLC program (ConsumeXml StopCmd)", "COM"],
    ["Platform", "tc online", "tc-template tc online", "Login + Start in one step", "COM"],
    ["Platform", "tc state", "tc-template tc state", "Show TwinCAT runtime state (Config/Run/Stop)", "COM"],
    ["Platform", "tc config", "tc-template tc config", "Switch to Config mode", "COM"],
    ["Platform", "tc run", "tc-template tc run", "Switch to Run mode", "COM"],
    ["Platform", "tc target", "tc-template tc target", "Show current target NetId", "COM"],
    ["Platform", "tc silent", "tc-template tc silent --on/--off", "Enable/disable silent mode (no dialogs)", "COM"],
    ["Platform", "tc deploy", "tc-template tc deploy", "Full pipeline: build->activate->restart->login->start", "COM + pyautogui"],
]

for row_idx, row_data in enumerate(commands):
    for col_idx, val in enumerate(row_data):
        cell = ws2.cell(row=row_idx + 1, column=col_idx + 1, value=val)
        cell.border = thin_border
        cell.alignment = wrap
        if row_idx == 0:
            cell.font = header_font
            cell.fill = header_fill
        elif row_idx in (1, 2, 3, 4, 5, 6, 7):
            pass  # Template
        elif row_idx == 8:
            pass  # PLC
        cell.font = body_font

# Group color bands
group_colors = {
    "Template": "E8F0FE",  # light blue
    "PLC": "E6F4EA",       # light green
    "Platform": "FFF3E0",  # light orange
}
for row_idx, row_data in enumerate(commands):
    if row_idx == 0:
        continue
    group = row_data[0]
    color = group_colors.get(group, "FFFFFF")
    fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
    for col_idx in range(5):
        ws2.cell(row=row_idx + 1, column=col_idx + 1).fill = fill

# Column widths
ws2.column_dimensions["A"].width = 14
ws2.column_dimensions["B"].width = 22
ws2.column_dimensions["C"].width = 50
ws2.column_dimensions["D"].width = 55
ws2.column_dimensions["E"].width = 22

# ================================================================
# Sheet 3: COM API Reference (CreateChild)
# ================================================================
ws3 = wb.create_sheet("COM Reference")

com_ref = [
    ["Object Type", "SubType", "vInfo", "File Ext", "Directory", "Notes"],
    ["Program", 603, "'ST'", ".TcPOU", "POUs", "vInfo='ST' as string (NOT array)"],
    ["Function Block", 604, "'ST'", ".TcPOU", "POUs", "Most common. vInfo='ST'"],
    ["Function", 603, "'ST'", ".TcPOU", "POUs", "Same subType as Program"],
    ["Struct (DUT)", 606, "''", ".TcDUT", "DUTs", "Write decl via filesystem"],
    ["Enum (DUT)", 605, "''", ".TcDUT", "DUTs", "Write decl via filesystem"],
    ["Union (DUT)", 607, "''", ".TcDUT", "DUTs", "Write decl via filesystem"],
    ["Alias (DUT)", 623, "''", ".TcDUT", "DUTs", "Write decl via filesystem"],
    ["GVL", 615, "None", ".TcGVL", "GVLs", "Declaration only, no implementation"],
    ["Interface", 618, "None", ".TcIO", "Interfaces", "Optional extend type"],
    ["Method", 609, "['ST', 'retType', 'PUBLIC']", "--", "--", "Child of POU"],
    ["Action", 608, "['ST']", "--", "--", "Child of POU"],
    ["Property", 611, "['ST', 'retType', 'PUBLIC']", "--", "--", "Child of POU"],
    ["POU Folder", 601, "None", "--", "--", "Organizational folder"],
    ["Visualization", 619, "None", ".TcVIS", "VISUs", "Blank visu canvas"],
]

for row_idx, row_data in enumerate(com_ref):
    for col_idx, val in enumerate(row_data):
        cell = ws3.cell(row=row_idx + 1, column=col_idx + 1, value=str(val) if val is not None else "None")
        cell.border = thin_border
        cell.alignment = wrap
        if row_idx == 0:
            cell.font = header_font
            cell.fill = header_fill
        else:
            cell.font = body_font

ws3.column_dimensions["A"].width = 20
ws3.column_dimensions["B"].width = 12
ws3.column_dimensions["C"].width = 30
ws3.column_dimensions["D"].width = 12
ws3.column_dimensions["E"].width = 14
ws3.column_dimensions["F"].width = 40

# ================================================================
# Sheet 4: Module Reference
# ================================================================
ws4 = wb.create_sheet("Modules")

modules = [
    ["Module", "Lines", "Purpose", "Dependencies"],
    ["tc_template/cli.py", "637", "Click CLI entry point, 3 command groups, 30+ commands", "All modules"],
    ["tc_template/extract.py", "595", "Template extraction + GUID protection + auto-description", "guid_utils, models, repository"],
    ["tc_template/plc.py", "556", "PLC code read/write + POU creation (filesystem + COM)", "models"],
    ["tc_template/tc_platform.py", "468", "Deploy pipeline + runtime control (COM + pyautogui)", "inspect"],
    ["tc_template/models.py", "309", "TemplateMetadata data model + YAML serialization", "pyyaml"],
    ["tc_template/guid_utils.py", "281", "GUID generation/scanning/classification/replacement", "models"],
    ["tc_template/repository.py", "223", "Template CRUD operations (default: Repository/)", "models"],
    ["tc_template/scaffold.py", "211", "Project scaffolding engine (placeholder replacement)", "guid_utils, models, repository"],
    ["tc_template/inspect.py", "195", "Error list reader (COM + clipboard)", "win32com, pyautogui"],
    ["tc_template/live_edit.py", "130", "pyautogui editor read/write/reload", "pyautogui, pyperclip"],
    ["tc_template/hooks.py", "101", "Lifecycle hook executor (Jinja2 conditions)", "jinja2"],
    ["tc_template/composer.py", "50", "Multi-template composition engine (WIP)", "models"],
    ["tc_template/__init__.py", "7", "Package initialization + version", "--"],
]

for row_idx, row_data in enumerate(modules):
    for col_idx, val in enumerate(row_data):
        cell = ws4.cell(row=row_idx + 1, column=col_idx + 1, value=val)
        cell.border = thin_border
        cell.alignment = wrap
        if row_idx == 0:
            cell.font = header_font
            cell.fill = header_fill
        else:
            cell.font = body_font

ws4.column_dimensions["A"].width = 30
ws4.column_dimensions["B"].width = 10
ws4.column_dimensions["C"].width = 55
ws4.column_dimensions["D"].width = 35

# ================================================================
# Save
# ================================================================
out = "docs/TwinCAT_Agent_Skills.xlsx"
wb.save(out)
print(f"Excel saved: {out}")
