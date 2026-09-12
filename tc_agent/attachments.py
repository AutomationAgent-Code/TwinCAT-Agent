"""
tc_agent.attachments —— 把用户附件转成模型能用的内容(给面板的"文件输入"用)。

两条链路,对应用户要的"两者都要":
  1) 文字提取(任何模型可用,含 DeepSeek 这类纯文本):
       PDF  → pypdf 抽正文(器件位号/端子/网络标号等文本层)
       xlsx → openpyxl 读成制表符表格(I/O 清单/端子表)
       csv/纯文本/代码 → 直接解码
  2) 视觉块(仅多模态模型,由 Provider 的 vision 开关决定是否发出):
       图片 → 原图;PDF → 交给原生支持的模型(Anthropic document 块)看图。

依赖 pypdf / openpyxl(+et_xmlfile),全是纯 Python —— 便携包可内置,不破坏免安装。
EPLAN 原生 .elk/.zw1 是私有二进制,不解析;实用做法是从 EPLAN 导出 PDF 或 Excel/CSV。
"""

from __future__ import annotations

import base64
import io
import os

# 每个文件抽取文字进模型上下文的上限(防大图纸/大表撑爆);总量另有上限。
PER_FILE_TEXT_CAP = 20000
TOTAL_TEXT_CAP = 60000
MAX_FILES = 8
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
# 单个视觉附件字节上限(base64 前);超了只走文字、不发图。
VISUAL_MAX_BYTES = 8 * 1024 * 1024
# 表格/PDF 读取的规模上限,避免超大文件卡住。
PDF_MAX_PAGES = 40
XLSX_MAX_ROWS = 800
XLSX_MAX_COLS = 40

# 按扩展名判定"当纯文本读"的类型。
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".xml", ".json", ".yaml",
    ".yml", ".ini", ".cfg", ".conf", ".st", ".scl", ".exp", ".il", ".var", ".gvl",
    ".py", ".c", ".h", ".cpp", ".hpp", ".cs", ".js", ".ts", ".html", ".css", ".sql",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class AttachmentCapabilityError(ValueError):
    """附件需要当前 Provider 未声明的模型能力。"""


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def _decode_text(raw: bytes) -> str:
    """尽量把字节解成文本:UTF-8 → GBK(国内 EPLAN/Excel 导出常见)→ latin-1 兜底。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return "[未安装 pypdf,无法抽取 PDF 文字]"
    try:
        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= PDF_MAX_PAGES:
                parts.append(f"…(仅取前 {PDF_MAX_PAGES} 页)")
                break
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t.strip():
                parts.append(f"— 第 {i + 1} 页 —\n{t.strip()}")
        if not parts:
            return "[该 PDF 没有可抽取的文字层(可能是扫描件或纯矢量图);如需读图请用多模态模型]"
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        return f"[PDF 解析失败: {e}]"


def _extract_xlsx(raw: bytes) -> str:
    try:
        import openpyxl
    except Exception:  # noqa: BLE001
        return "[未安装 openpyxl,无法读取 Excel]"
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            rows = []
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= XLSX_MAX_ROWS:
                    rows.append(f"…(仅取前 {XLSX_MAX_ROWS} 行)")
                    break
                cells = ["" if v is None else str(v) for v in row[:XLSX_MAX_COLS]]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells).rstrip())
            if rows:
                out.append(f"【工作表 {ws.title}】\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(out) if out else "[Excel 为空]"
    except Exception as e:  # noqa: BLE001
        return f"[Excel 解析失败: {e}]"


def _kind_of(name: str, mime: str) -> str:
    ext, mime = _ext(name), (mime or "").lower()
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in (".xlsx", ".xlsm") or "spreadsheetml" in mime:
        return "xlsx"
    if ext in IMAGE_EXTS or mime in IMAGE_MIMES:
        return "image"
    if ext in TEXT_EXTS or mime.startswith("text/"):
        return "text"
    return "other"


def _image_media_type(name: str, mime: str) -> str:
    mime = (mime or "").lower()
    if mime in IMAGE_MIMES:
        return "image/jpeg" if mime == "image/jpg" else mime
    ext = _ext(name)
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")


def process(attachments: list, vision: bool) -> dict:
    """把一批附件({name, mime, size, data_b64})转成:
       {"text": 合并后的附件正文(带文件名分节,已限长),
        "visual": [中间视觉块], 仅 vision=True 时非空,
        "meta": [{name, kind, note}] 供 UI 展示/回放}
    visual 中间块形如 {"kind":"image"|"pdf", "media_type":..., "data_b64":..., "name":...},
    由 agent_core 按各家协议翻译成 image_url / image / document。"""
    text_chunks: list[str] = []
    visual: list[dict] = []
    meta: list[dict] = []
    total = 0
    total_bytes = 0

    if len(attachments or []) > MAX_FILES:
        raise ValueError(f"一次最多上传 {MAX_FILES} 个附件")

    for att in attachments or []:
        name = att.get("name") or "未命名"
        mime = att.get("mime") or ""
        b64 = att.get("data_b64") or ""
        try:
            raw = base64.b64decode(b64) if b64 else b""
        except Exception:  # noqa: BLE001
            meta.append({"name": name, "kind": "error", "note": "base64 解码失败"})
            continue
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"附件《{name}》超过单文件 12MB 上限")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("本次附件总大小超过 30MB 上限")

        kind = _kind_of(name, mime)
        note = ""
        body = ""

        if kind == "pdf":
            body = _extract_pdf(raw)
            if body.startswith("[该 PDF 没有可抽取的文字层"):
                if not vision:
                    raise AttachmentCapabilityError(
                        f"《{name}》没有可抽取的文字，可能是扫描件或纯图纸。"
                        "当前模型不是多模态模型，请切换到支持读图/PDF的 Provider。"
                    )
                if len(raw) > VISUAL_MAX_BYTES:
                    raise ValueError(f"扫描版 PDF《{name}》超过视觉输入 8MB 上限")
            if vision and len(raw) <= VISUAL_MAX_BYTES:
                visual.append({"kind": "pdf", "media_type": "application/pdf",
                               "data_b64": b64, "name": name})
                note = "已抽文字 + 随图发给模型"
            else:
                note = "已抽文字" + ("(超 8MB 未发图)" if vision else "")
        elif kind == "xlsx":
            body = _extract_xlsx(raw)
            note = "已读表格"
        elif kind == "text":
            body = _decode_text(raw)
            note = "已读文本"
        elif kind == "image":
            if not vision:
                raise AttachmentCapabilityError(
                    f"当前模型不支持图片附件《{name}》。"
                    "请切换到支持读图的多模态 Provider，或把内容导出为 Excel/CSV/文本。"
                )
            if len(raw) > VISUAL_MAX_BYTES:
                raise ValueError(f"图片《{name}》超过视觉输入 8MB 上限")
            visual.append({"kind": "image", "media_type": _image_media_type(name, mime),
                           "data_b64": b64, "name": name})
            note = "随图发给模型(多模态)"
        else:
            note = "不支持的类型,已忽略(EPLAN 请导出 PDF/Excel)"

        if body.strip():
            body = body.strip()
            if len(body) > PER_FILE_TEXT_CAP:
                body = body[:PER_FILE_TEXT_CAP] + f"\n…(该文件文字过长,已截断到 {PER_FILE_TEXT_CAP} 字)"
            if total + len(body) > TOTAL_TEXT_CAP:
                room = max(0, TOTAL_TEXT_CAP - total)
                body = body[:room] + "\n…(附件总量超限,后续截断)"
            total += len(body)
            text_chunks.append(f"### 附件:{name}（{kind}）\n{body}")

        meta.append({"name": name, "kind": kind, "note": note})

    combined = ""
    if text_chunks:
        combined = ("\n\n===== 用户上传的附件（以下为自动提取的内容，供你参考）=====\n\n"
                    + "\n\n".join(text_chunks)
                    + "\n\n===== 附件结束 =====")
    return {"text": combined, "visual": visual, "meta": meta}
