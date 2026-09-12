"""Generate TwinCAT Agent presentation PPT."""
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()
prs.slide_width = Inches(13.33)
prs.slide_height = Inches(7.5)

# Colors
DARK = RGBColor(0x1A, 0x1A, 0x2E)
BLUE = RGBColor(0x00, 0x50, 0x99)
LIGHT_BLUE = RGBColor(0x00, 0x78, 0xD4)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_GRAY = RGBColor(0xF5, 0xF5, 0xF5)
DARK_TEXT = RGBColor(0x33, 0x33, 0x33)

def add_dark_bg(slide):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = DARK

def add_title(slide, text, x=0.5, y=0.3, w=12, h=1.0, size=40):
    txBox = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    p = txBox.text_frame.paragraphs[0]
    p.text = text
    p.font.size = Pt(size)
    p.font.color.rgb = WHITE
    p.font.bold = True

def add_subtitle(slide, text, x=0.5, y=1.4, w=12, h=0.8, size=22):
    txBox = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    p = txBox.text_frame.paragraphs[0]
    p.text = text
    p.font.size = Pt(size)
    p.font.color.rgb = LIGHT_BLUE
    p.font.bold = False

def add_body(slide, text, x=0.5, y=2.2, w=12, h=5, size=18):
    txBox = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, line in enumerate(text.split('\n')):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.font.size = Pt(size)
        p.font.color.rgb = WHITE
        p.space_after = Pt(6)

def add_table(slide, headers, rows, x=0.5, y=2.5, w=12, h=4.5):
    n_rows = len(rows) + 1
    n_cols = len(headers)
    table_shape = slide.shapes.add_table(n_rows, n_cols, Inches(x), Inches(y), Inches(w), Inches(h))
    table = table_shape.table
    for i, h in enumerate(headers):
        cell = table.cell(0, i)
        cell.text = h
        for p in cell.text_frame.paragraphs:
            p.font.size = Pt(14)
            p.font.bold = True
            p.font.color.rgb = WHITE
        cell.fill.solid()
        cell.fill.fore_color.rgb = BLUE
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = table.cell(ri + 1, ci)
            cell.text = str(val)
            for p in cell.text_frame.paragraphs:
                p.font.size = Pt(13)
                p.font.color.rgb = DARK_TEXT
            cell.fill.solid()
            cell.fill.fore_color.rgb = LIGHT_GRAY if ri % 2 == 0 else WHITE

# ===== Slide 1: Cover =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "TwinCAT Agent", x=1, y=1.5, w=11, h=1.2, size=54)
add_subtitle(slide, "AI 驱动的 TwinCAT 3 自动化开发平台", x=1, y=2.8, w=11, h=0.8, size=28)
add_body(slide, "AI 语音编程 * 模板管理 * 一键部署\n\n田然 | 2026 年", x=1, y=4.2, w=11, h=2, size=20)

# ===== Slide 2: Pain Points =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "痛点与目标", size=38)
add_body(slide, """痛点:
  > 新建项目繁琐 -- 每次都从头创建，没有复用模板
  > GUID 噩梦 -- 复制项目时 GUID 冲突导致 UnknownObject 崩溃
  > PLC 编程效率低 -- 代码在各种文件之间手动复制
  > 编译部署步骤多 -- Build->Activate->Restart->Login->Start
  > 弹窗需人工确认 -- 无法无人值守部署

目标: 让 AI 完成重复劳动，开发者专注核心逻辑""", x=0.8, y=1.8, w=11, h=5.5, size=20)

# ===== Slide 3: Architecture =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "系统架构", size=38)
add_body(slide, """                          +-----------------------------------+
                          |       Claude Code (AI Agent)        |
                          | /twincat-template  /plc  /tc       |
                          +----------------+------------------+
                                           |
              +----------------------------+---------------------------+
              |                            |                           |
+-------------v------+  +-----------------v----+  +-------------------v---+
|    Filesystem      |  |  COM Automation     |  |     pyautogui         |
|    XML CDATA       |  |  win32com.DTE       |  |  Ctrl+A/C/V/S        |
|  .TcPOU .TcDUT ... |  |  ITcSysManager     |  |  Mouse Click          |
+--------------------+  +---------------------+  +-----------------------+
              |                            |                           |
              +----------------------------+---------------------------+
                                           |
                        +------------------v-------------------+
                        |         TwinCAT 3 XAE                |
                        |        (TcXaeShell)                  |
                        +--------------------------------------+""", x=0.5, y=1.5, w=12, h=5.5, size=13)

# ===== Slide 4: Three Skills =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "三大 Skill -- 30+ 命令覆盖全生命周期", size=36)
add_table(slide,
    ["Skill", "Commands", "Function", "Tech"],
    [
        ["/twincat-template", "7", "14 templates, create/extract/validate, GUID protection", "GUID whitelist + XML attrs"],
        ["/plc", "12", "POU/DUT/GVL CRUD, Live Edit, export/import", "COM create + FS write + pyautogui"],
        ["/tc", "15", "Build/Activate/Login/Start/Mode switch/Deploy", "COM + ConsumeXml + dialog dismiss"],
    ],
    y=2.2, h=3.5)

# ===== Slide 5: GUID Protection =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Template Management -- GUID Protection", size=36)
add_table(slide,
    ["Replaced (project GUID -> {{GUID_N}})", "Preserved (Beckhoff system GUID)"],
    [
        ["Id=\"...\" attribute", "{00000000-...} null GUID (TwinCAT sentinel)"],
        ["ProjectGUID=\"...\" / ProjectGuid=\"...\"", "{18071995-*} Beckhoff Type System"],
        ["<Application>/<TypeSystem>/<LibraryReferences>", "{B1E792BE-...} TcXaeShell project type"],
        ["GuidA / GuidB / TmcHash", "{08500001-...} TcPlc30 CLSID"],
        ["FbGuid, OptionKey GUIDs", "<ProjectExtensions> section ALL GUIDs"],
        ["", "<Licenses>, <Device>, <Placeholder*> sections"],
    ],
    y=2.0, h=5.0)

# ===== Slide 6: PLC Programming =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "PLC Programming -- Three-Layer Approach", size=36)
add_table(slide,
    ["Operation", "COM", "Filesystem", "pyautogui", "Best"],
    [
        ["List objects", "--", "*****", "--", "Filesystem"],
        ["Read code", "--", "*****", "****", "Filesystem"],
        ["Write code", "--", "*****", "****", "Filesystem + pyautogui"],
        ["Create POU", "*****", "-- (plcproj edit)", "-- (unreliable)", "COM"],
        ["Error List", "-- (no DTE2)", "--", "****", "pyautogui Ctrl+A/C"],
    ],
    y=2.0, h=3.5)

add_body(slide, "COM CreateChild:  pous.CreateChild('FB_Name', 604, '', 'ST')  -- vInfo='ST' string format!\nFB=604 | Program=603 | Struct=606 | Enum=605 | GVL=615 | Visu=619",
    x=0.5, y=5.8, w=12, h=1.5, size=16)

# ===== Slide 7: Deploy =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "tc deploy -- One-Click Deploy", size=36)
add_body(slide, """tc deploy -> 5 steps fully automated:

  Build --> Activate --> Restart --> Login --> Start
    |         |           |           |          |
    |    ActivateConfig   |     LoginCmd + StartCmd
    |    (SaveToRegistry) |     ConsumeXml on PLC Instance
    |                     |
    +-- pyautogui background thread dismisses dialogs --+
       SilentMode ON + clicks OK every 0.4 seconds

  Result: type tc deploy -> 10 seconds later PLC is online""",
    x=0.8, y=1.8, w=11, h=5.5, size=18)

# ===== Slide 8: Live Editing =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Live Editing -- Code Like a Human Programmer", size=36)
add_body(slide, """Read:
   pyautogui click editor -> Ctrl+A -> Ctrl+C -> pyperclip.paste()

Write:
   pyperclip.copy(code) -> pyautogui click -> Ctrl+A -> Ctrl+V -> Ctrl+S

Editor layout:
   +------------------------+
   | Declaration (upper 25%)|  <- Variables, I/O
   +------------------------+
   | Implementation (lower) |  <- ST code
   +------------------------+

CDATA preservation: marker-based serialization, no escaping <![CDATA[...]]>""",
    x=0.8, y=1.8, w=11, h=5.5, size=18)

# ===== Slide 9: Stats =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Project Statistics", size=36)
add_table(slide,
    ["Metric", "Value"],
    [
        ["Python Modules", "13 files, 3,763 lines of code"],
        ["CLI Commands", "3 command groups, 30+ commands"],
        ["Skills", "3 (/twincat-template, /plc, /tc)"],
        ["Knowledge Base", "3 docs (TC3 API ref + 243pg tutorial + AI workflow)"],
        ["Templates", "14 (packml + model1 + 12 SPT samples)"],
        ["Core Modules", "cli.py(637L), extract.py(595L), plc.py(556L), tc_platform.py(468L)"],
    ],
    y=2.0, h=3.5)

# ===== Slide 10: Breakthroughs =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Technical Breakthroughs", size=36)
add_body(slide, """1. GUID Whitelist Protection -- attribute-level match vs full-file scan, fixed Type System GUID false positives

2. COM POU Creation -- discovered vInfo='ST' string format (not array ['6']), avoids NWL ladder diagram creation

3. SilentMode + Background Click -- solves TwinCAT dialog blocking COM calls, enables unattended deployment

4. CDATA Marker Serialization -- solves Python XML lib auto-escaping CDATA that TwinCAT cannot parse

5. Three-Layer Tool Architecture -- Filesystem(most reliable) / COM(only for creation) / pyautogui(real-time interaction)""",
    x=0.8, y=1.8, w=11, h=5.5, size=18)

# ===== Slide 11: Demo =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Live Demo", size=36)
add_body(slide, """Scenario 1: Create project and deploy
  $ tc-template create packml -n MyProject -o G:/Prj
    Created: 37 files, 223 GUIDs
  $ tc deploy
    Build: 0 errors. Online: Login + Start OK. PLC Running.

Scenario 2: AI coding -- Traffic light FB
  $ tc-template plc create-com FB_TrafficLight -t functionBlock
  $ tc-template plc write ... "timer(IN:=bEnable, PT:=T#3S);"
  $ tc-template plc write ... MAIN "fbTL : FB_TrafficLight;"
  $ tc deploy

Scenario 3: Template extraction (14-template library)
  $ tc-template add reference/MyProject -n my-plc""",
    x=0.8, y=1.8, w=11, h=5.5, size=17)

# ===== Slide 12: Roadmap =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Roadmap", size=36)
add_table(slide,
    ["Priority", "Feature", "Description"],
    [
        ["P0", "Code diff tool", "git diff .TcPOU files"],
        ["P0", "Template auto-update", "Sync project changes back to template"],
        ["P1", "Library version management", "Auto-resolve Placeholder -> installed version"],
        ["P1", "VISU programming", "HMI elements via COM + property binding"],
        ["P2", "Batch project build", "For CI/CD pipeline"],
        ["P2", "Remote target deploy", "SetTargetNetId + cross-network deploy"],
    ],
    y=2.0, h=3.5)

# ===== Slide 13: Summary =====
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_dark_bg(slide)
add_title(slide, "Summary", size=44)
add_subtitle(slide, "AI handles repetition, developers focus on core logic", x=0.8, y=1.6, w=11, h=0.8, size=26)
add_body(slide, """Three Pillars:
  > Template Management -- 14 templates + GUID whitelist + auto-description
  > PLC Programming -- COM create + filesystem read/write + pyautogui live edit
  > Platform Control -- one-click deploy + auto dialog dismissal + mode switching

Technical Highlights:
  > Three-layer tool complement (COM / Filesystem / pyautogui)
  > Full lifecycle coverage (Create -> Code -> Build -> Deploy)
  > Production-verified (tested with TcXaeShell 15.0)""",
    x=1.5, y=2.8, w=10, h=4, size=20)

# Save
out = "docs/TwinCAT_Agent_Presentation.pptx"
prs.save(out)
print(f"PPT saved: {out}")
