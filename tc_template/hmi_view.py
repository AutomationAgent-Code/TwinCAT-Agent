# -*- coding: utf-8 -*-
"""TE2000 HMI .view/.content 无重载编辑 (走 VS DTE TextDocument, IDE 内部)。

痛点: 直接写磁盘 .view 文件, VS 检测到外部改动 -> 弹"文件已修改"要求重载。
解法: 和 PLC 的 COM 写入同理 —— 通过 DTE 打开文档、编辑其文本缓冲、doc.Save(),
      VS 视为内部编辑, 不弹重载框、不需手动 reload。

    from tc_template.hmi_view import read_view, write_view, replace_in_view
    text = read_view(r'...\\Desktop.view')
    write_view(r'...\\Desktop.view', new_html)          # 整体替换
    replace_in_view(r'...\\Desktop.view', old, new)      # 局部替换
"""
import win32com.client

_VS_TEXTVIEW = '{7651A703-06E5-11D1-8EBD-00A0C90F26EA}'  # vsViewKindTextView


def _dte():
    from ._com import force_dynamic_dispatch
    force_dynamic_dispatch()
    for pid in ('TcXaeShell.DTE.15.0', 'TcXaeShell.DTE.14.0', 'VisualStudio.DTE.17.0'):
        try:
            return win32com.client.GetActiveObject(pid)
        except Exception:
            continue
    raise RuntimeError('No TwinCAT/VS running. Open TcXaeShell first.')


def _open_textdoc(path, dte=None):
    import os, time
    dte = dte or _dte()
    target = os.path.normcase(os.path.abspath(path))

    def _find_doc():
        # 优先按 FullName 精确匹配已打开文档 (ActiveDocument 可能是设计器/别的窗口)
        try:
            docs = dte.Documents
            for i in range(1, docs.Count + 1):
                d = docs.Item(i)
                try:
                    if os.path.normcase(os.path.abspath(d.FullName)) == target:
                        return d
                except Exception:
                    continue
        except Exception:
            pass
        try:
            d = dte.ActiveDocument
            if d is not None:
                return d
        except Exception:
            pass
        return None

    dte.ItemOperations.OpenFile(path, _VS_TEXTVIEW)
    last = None
    for _ in range(20):
        doc = _find_doc()
        if doc is not None:
            try:
                td = doc.Object('TextDocument')
                if td is not None:
                    return doc, td
            except Exception as e:
                last = e
        time.sleep(0.3)
    raise RuntimeError(f"Could not get TextDocument for {path}: {last}")


def read_view(path: str) -> str:
    """读取 .view/.content 当前(IDE 缓冲区)文本。"""
    _doc, td = _open_textdoc(path)
    ep = td.StartPoint.CreateEditPoint()
    return ep.GetText(td.EndPoint)


def write_view(path: str, content: str) -> dict:
    """整体替换 .view/.content 文本并保存 —— IDE 内部编辑, 无外部重载弹窗。"""
    from ._ps_bridge import com_hmi_write_markup
    return com_hmi_write_markup(path, content, apply=True)


def replace_in_view(path: str, old: str, new: str, count: int = 0) -> dict:
    """在 .view/.content 里做字符串替换(局部)并保存, 无重载。count=0 表示全部。"""
    text = read_view(path)
    if old not in text:
        return {'status': 'not_found', 'path': path}
    text = text.replace(old, new) if count == 0 else text.replace(old, new, count)
    return write_view(path, text)
