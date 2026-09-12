"""FB 库引擎 —— 把常用功能块封装成可参数化文本模板，一键塞入/反向抽取。

见 docs/fblib_architecture.md。存储：fblib/<slug>/ 下 manifest.yaml + <FB>.decl/.impl
+ types/*.decl。塞入走 com_new_pou + com_add_library，收进库前过 plc lint。

纯函数（离线可测）：list_fbs / get_fb / render_fb / validate_fb。
COM 部分（需 XAE）：add_fb / extract_fb。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def fblib_dir() -> Path:
    """FB 库根目录（仓库根下的 fblib/）。"""
    return Path(__file__).resolve().parent.parent / "fblib"


# ---------------------------------------------------------------- 读取

def _read(p: Path) -> str:
    # utf-8-sig: 剥掉 BOM。Windows 工具(PowerShell Set-Content -Encoding utf8 等)
    # 写出的文件常带 BOM, 若原样送进 CreateChild 会让 TwinCAT 报
    # 「"PROPERTY" expected instead of ﻿」这类诡异语法错。
    return p.read_text(encoding="utf-8-sig") if p.exists() else ""


def load_catalog(root: Path | None = None) -> dict:
    """读取 fblib/catalog.yaml —— 分类定义 + 选型路由规则。缺失时返回空结构。"""
    root = root or fblib_dir()
    f = root / "catalog.yaml"
    if not f.exists():
        return {"categories": [], "routes": []}
    return yaml.safe_load(_read(f)) or {"categories": [], "routes": []}


def list_fbs(category: str | None = None, root: Path | None = None) -> list[dict]:
    """列出 FB 库中所有模板的摘要（含选型元数据 keywords/use_when/family）。

    排序：先按 catalog.yaml 里的分类顺序，同类内 rank 小的在前（旗舰模板优先）。
    """
    root = root or fblib_dir()
    out: list[dict] = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        mf = d / "manifest.yaml"
        if not d.is_dir() or not mf.exists():
            continue
        # 单个模板的 manifest 写坏了不该让整个列表崩掉 —— 标记出来跳过。
        try:
            m = yaml.safe_load(_read(mf)) or {}
        except yaml.YAMLError as e:
            out.append({"slug": d.name, "name": "", "category": "",
                        "archetype": "", "description": f"[manifest 解析失败] {e}"})
            continue
        if category and m.get("category") != category:
            continue
        out.append({
            "slug": m.get("slug", d.name),
            "name": m.get("name", ""),
            "category": m.get("category", ""),
            "archetype": m.get("archetype", ""),
            "family": m.get("family", ""),
            "rank": m.get("rank", 50),
            "keywords": m.get("keywords", []) or [],
            "use_when": m.get("use_when", ""),
            "description": m.get("description", ""),
        })

    order = {c["name"]: i for i, c in enumerate(
        load_catalog(root).get("categories", []) or [])}
    out.sort(key=lambda f: (order.get(f.get("category"), 99),
                            f.get("rank", 50), f.get("slug", "")))
    return out


def find_fbs(intent: str, root: Path | None = None, limit: int = 5) -> dict:
    """按意图检索模板 —— 「我要写轴程序」→ 首选 spt-axis-basic。

    打分：分类路由命中 +10；模板 keywords 命中 +4 且每多命中一个再 +2（命中越
    具体越靠前 —— 「回零」要能压过同分类的旗舰通用模板）；slug/name 子串 +4；
    描述 +2；最后减 rank/100 做同分决胜，让旗舰模板在纯泛泛的问法下胜出。
    返回 {intent, matched_categories, results:[{... , score, why}]}。
    """
    root = root or fblib_dir()
    text = (intent or "").lower()
    catalog = load_catalog(root)

    # 1) 意图 → 分类（catalog.routes）
    hit_cats: dict[str, list[str]] = {}
    for rt in catalog.get("routes", []) or []:
        hits = [k for k in (rt.get("when", []) or []) if k.lower() in text]
        if hits:
            hit_cats.setdefault(rt["category"], []).extend(hits)

    results = []
    for f in list_fbs(root=root):
        score, why = 0.0, []
        if f["category"] in hit_cats:
            score += 10
            why.append(f"分类命中({'/'.join(sorted(set(hit_cats[f['category']])))})")
        kw = [k for k in f["keywords"] if str(k).lower() in text]
        if kw:
            score += min(4 + 2 * (len(kw) - 1), 12)
            why.append(f"关键词({'/'.join(kw)})")
        if f["slug"].lower() in text or (f["name"] and f["name"].lower() in text):
            score += 4
            why.append("名称命中")
        if any(w and w in f["description"].lower() for w in text.split()):
            score += 2
            why.append("描述命中")
        if score:
            results.append({**f, "score": round(score - f.get("rank", 50) / 100, 2),
                            "why": "; ".join(why)})

    results.sort(key=lambda r: -r["score"])
    return {"intent": intent, "matched_categories": sorted(hit_cats),
            "results": results[:limit],
            "hint": ("没匹配到模板 —— 用 fblib list 看全量，"
                     "确实没有再手写，写完可用 fblib extract 收进库")
            if not results else
            "优先用排第一的模板 fblib add <slug>，不要从零手写"}


def get_fb(slug: str, root: Path | None = None) -> dict:
    """加载一个模板的完整内容：manifest + decl/impl + 配套类型（原始 {{占位符}}）。"""
    root = root or fblib_dir()
    d = root / slug
    mf = d / "manifest.yaml"
    if not mf.exists():
        raise FileNotFoundError(f"FB 模板不存在: {slug} ({mf})")
    m = yaml.safe_load(_read(mf)) or {}
    name = m.get("name", "")

    fmt = m.get("format", "text")
    result: dict[str, Any] = {"manifest": m, "format": fmt}
    if fmt == "plcopen":
        result["plcopen_xml"] = _read(d / f"{name}.xml")
        return result

    result["declaration"] = _read(d / f"{name}.decl")
    result["implementation"] = _read(d / f"{name}.impl")
    companions = []
    for comp in m.get("companions", []) or []:
        companions.append({"name": comp, "declaration": _read(d / "types" / f"{comp}.decl")})
    result["companions"] = companions
    return result


# ---------------------------------------------------------------- 渲染

def _render(text: str, values: dict[str, str]) -> str:
    """把 {{NAME}} 占位符替换成 values[NAME]。未知占位符原样保留。"""
    def sub(mo: re.Match) -> str:
        key = mo.group(1).strip()
        return values.get(key, mo.group(0))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, text)


def _param_values(manifest: dict, params: dict[str, str] | None) -> dict[str, str]:
    """合并用户参数与 manifest 默认值。"""
    params = params or {}
    vals: dict[str, str] = {}
    for p in manifest.get("params", []) or []:
        vals[p["name"]] = params.get(p["name"], str(p.get("default", "")))
    # 允许传入 manifest 未声明的额外键
    for k, v in params.items():
        vals.setdefault(k, str(v))
    return vals


def _render_objects(d: Path, manifest: dict, vals: dict[str, str]) -> list[dict]:
    """渲染 objects: 多对象模板（OOP：接口 + FB + 方法/属性）。

    目录约定：<对象名>.decl / <对象名>.impl；成员在 <对象名>/<成员>.decl|.impl，
    属性访问器为 <对象名>/<属性>.Get.impl / .Set.impl。

    对象名本身也过占位符渲染（``name: "{{MODULE_NAME}}"``），这样同一套模板能按
    用户起的名字落地；磁盘上的文件名取 ``file:``（没写就用 name），保持干净。
    """
    objs: list[dict] = []
    for spec in manifest.get("objects", []) or []:
        oname = spec.get("file") or spec["name"]   # 文件名/目录名
        obj: dict[str, Any] = {
            "name": _render(spec["name"], vals),
            "kind": spec.get("kind", "fb"),
            "return_type": spec.get("return_type", ""),
            "declaration": _render(_read(d / f"{oname}.decl"), vals),
            "implementation": _render(_read(d / f"{oname}.impl"), vals),
            "members": [],
        }
        for msp in spec.get("members", []) or []:
            mname = msp["name"]
            # YAML 1.1 陷阱: 裸写的 On/Off/Yes/No/True/False 会被解析成布尔,
            # 成员名会变成 'True'/'False' 而在 CreateChild 处报「名称不匹配」。
            if not isinstance(mname, str):
                raise ValueError(
                    f"{spec['name']} 的成员名 {mname!r} 不是字符串 —— "
                    f"YAML 把 On/Off/Yes/No 当布尔了, 请在 manifest 里加引号")
            mkind = msp.get("kind", "method")
            mdir = d / oname
            mfile = msp.get("file") or mname
            member = {
                "name": _render(mname, vals),
                "kind": mkind,
                "return_type": msp.get("return_type", "BOOL"),
                "declaration": _render(_read(mdir / f"{mfile}.decl"), vals),
                "implementation": _render(_read(mdir / f"{mfile}.impl"), vals),
                "accessors": [],
            }
            # 属性访问器：优先取 <属性>.Get.impl / <属性>.Set.impl 的实现；
            # 接口属性没有实现文件, 但【必须】建访问器节点才可用(否则编译报
            # 「'X' is no component of 'I_Xxx__Union'」), 故支持在 manifest 里
            # 用 accessors: [Get, Set] 声明, 属性默认建 Get。
            declared = msp.get("accessors")
            for acc in ("Get", "Set"):
                txt = _read(mdir / f"{mfile}.{acc}.impl")
                if txt:
                    member["accessors"].append(
                        {"name": acc, "implementation": _render(txt, vals)})
                elif mkind == "property" and (
                        (declared and acc in declared)
                        or (declared is None and acc == "Get")):
                    member["accessors"].append({"name": acc, "implementation": ""})
            obj["members"].append(member)
        objs.append(obj)
    return objs


def render_fb(slug: str, params: dict[str, str] | None = None,
              root: Path | None = None) -> dict:
    """渲染一个模板（占位符已替换），返回可直接塞入的内容。

    Returns {name, archetype, format, declaration, implementation,
             companions:[{name, kind, declaration}], libraries, methods}.
    """
    fb = get_fb(slug, root=root)
    m = fb["manifest"]
    vals = _param_values(m, params)
    out: dict[str, Any] = {
        "name": m.get("name", ""),
        "archetype": m.get("archetype", ""),
        "format": fb["format"],
        "libraries": m.get("libraries", []) or [],
        "methods": m.get("methods", []) or [],
        "requires": m.get("requires", []) or [],   # 依赖的其他 fblib 模板 slug
        "style": m.get("style", "default"),        # default | spt (lint 规则集)
        "notes": m.get("notes", ""),
    }
    if fb["format"] == "plcopen":
        out["plcopen_xml"] = _render(fb["plcopen_xml"], vals)
        return out

    # objects 模式（OOP：接口 + FB + 方法/属性），有则优先
    if m.get("objects"):
        from pathlib import Path as _P
        d = (root or fblib_dir()) / slug
        out["objects"] = _render_objects(_P(d), m, vals)
        out["companions"] = []
        out["declaration"] = ""
        out["implementation"] = ""
        return out

    out["declaration"] = _render(fb["declaration"], vals)
    out["implementation"] = _render(fb["implementation"], vals)
    out["companions"] = [
        {"name": c["name"], "kind": _dut_kind(c["declaration"]),
         "declaration": _render(c["declaration"], vals)}
        for c in fb["companions"]
    ]
    return out


def _dut_kind(decl: str) -> str:
    """从 DUT 声明判定 enum / struct / union。"""
    u = decl.upper()
    if re.search(r"\bSTRUCT\b", u):
        return "struct"
    if re.search(r"\bUNION\b", u):
        return "union"
    return "enum"   # TYPE X : ( ... ) 形式


# ---------------------------------------------------------------- 校验（离线 lint）

def validate_fb(slug: str, params: dict[str, str] | None = None,
                root: Path | None = None) -> dict:
    """离线校验模板：渲染后按 lint 规则检查（不需 XAE）。"""
    from .lint import lint_objects, summarize
    r = render_fb(slug, params=params, root=root)
    if r["format"] == "plcopen":
        return {"slug": slug, "format": "plcopen",
                "note": "plcopen 模板跳过文本 lint", "findings": []}

    style = r.get("style", "default")
    if r.get("objects"):
        objs = []
        for o in r["objects"]:
            folder = "DUTs" if o["kind"] in ("enum", "struct", "union") else "POUs"
            objs.append({"name": o["name"], "folder": folder,
                         "declaration": o["declaration"],
                         "implementation": o["implementation"],
                         "methods": [{"name": mm["name"],
                                      "declaration": mm["declaration"],
                                      "implementation": mm["implementation"]}
                                     for mm in o["members"]]})
        findings = lint_objects(objs, style=style)
        return {"slug": slug, "style": style,
                "summary": summarize(findings), "findings": findings}

    objs = [{"name": r["name"], "folder": "POUs",
             "declaration": r["declaration"], "implementation": r["implementation"],
             "methods": []}]
    for c in r["companions"]:
        objs.append({"name": c["name"], "folder": "DUTs",
                     "declaration": c["declaration"], "implementation": "",
                     "methods": []})
    findings = lint_objects(objs, style=style)
    return {"slug": slug, "style": style,
            "summary": summarize(findings), "findings": findings}


# ---------------------------------------------------------------- 塞入（COM）

def add_fb(slug: str, params: dict[str, str] | None = None,
           add_libraries: bool = True, run_lint: bool = True,
           root: Path | None = None) -> dict:
    """把模板塞进当前打开的 PLC 项目（COM）。

    顺序：建配套类型 → 建 FB → 加库依赖 → plc lint。
    """
    from . import _ps_bridge as ps
    r = render_fb(slug, params=params, root=root)
    created: dict[str, Any] = {"fb": r["name"], "companions": [], "libraries": []}

    if r["format"] == "plcopen":
        raise NotImplementedError("plcopen 格式塞入走 com_import_plcopen，v1 暂未接线")

    if r.get("objects"):
        # OOP 模式：按 manifest 顺序建对象（枚举→接口→FB），再建各自成员。
        created["objects"] = []

        # 依赖模板先塞（如 cylinder 依赖 digital-output 的 I_DigitalOutput）
        for dep in r.get("requires", []):
            sub = add_fb(dep, params=params, add_libraries=add_libraries,
                         run_lint=False, root=root)
            created.setdefault("required", []).append(
                {"slug": dep, "objects": sub.get("objects", [])})

        # 已存在的对象跳过（依赖模板可能已带入，或用户项目里已有同名对象）
        try:
            existing = {o.get("name") for o in ps.com_all_code()}
        except Exception:
            existing = set()

        for o in r["objects"]:
            if o["name"] in existing:
                created.setdefault("objects_skipped", []).append(o["name"])
                continue
            ps.com_new_pou(o["name"], pou_type=o["kind"],
                           declaration=o["declaration"],
                           implementation=o["implementation"], style=r.get("style", "default"))
            created["objects"].append(o["name"])
            for mem in o["members"]:
                try:
                    ps.com_new_member(o["name"], mem["name"],
                                      member_type=mem["kind"],
                                      return_type=mem.get("return_type", "BOOL"),
                                      declaration=mem["declaration"],
                                      implementation=mem["implementation"],
                                      style=r.get("style", "default"))
                except Exception as exc:
                    raise RuntimeError(
                        f"creating {o['kind']} {o['name']}.{mem['name']} "
                        f"({mem['kind']}, return={mem.get('return_type', 'BOOL')}): {exc}"
                    ) from exc
                # 属性访问器：TwinCAT 建 property 时【不会】自动带 Get/Set,
                # 必须先显式建访问器节点(613/614), 再用点号路径写实现。
                for acc in mem.get("accessors", []):
                    try:
                        ps.com_new_member(o["name"], mem["name"],
                                          member_type=("propget" if acc["name"] == "Get"
                                                       else "propset"),
                                          return_type=mem.get("return_type", "BOOL"),
                                          style=r.get("style", "default"))
                    except Exception as exc:
                        raise RuntimeError(
                            f"creating accessor {o['name']}.{mem['name']}.{acc['name']}: {exc}"
                        ) from exc
                    if acc["implementation"]:      # 接口访问器无实现体
                        ps.com_write_pou(o["name"], acc["implementation"],
                                         area="implementation",
                                         method_name=f"{mem['name']}.{acc['name']}",
                                         style=r.get("style", "default"))
    else:
        # 配套类型先建（FB 声明引用它们）
        for c in r["companions"]:
            ps.com_new_pou(c["name"], pou_type=c["kind"],
                           declaration=c["declaration"], implementation="",
                           style=r.get("style", "default"))
            created["companions"].append(c["name"])
        # 建 FB
        ps.com_new_pou(r["name"], pou_type="fb",
                       declaration=r["declaration"], implementation=r["implementation"],
                       style=r.get("style", "default"))
    # 加库依赖（幂等：已存在的库跳过，避免重复引用）
    if add_libraries and r["libraries"]:
        try:
            existing = {l.get("name") for l in ps.com_list_libraries()}
        except Exception:
            existing = set()
        for lib in r["libraries"]:
            if lib in existing:
                created.setdefault("libraries_skipped", []).append(lib)
                continue
            try:
                ps.com_add_library(lib)
                created["libraries"].append(lib)
            except Exception as e:
                created.setdefault("library_warnings", []).append(f"{lib}: {e}")
    # 合规门禁
    if run_lint:
        from .lint import lint_objects, summarize
        # objects 模式下 r["name"] 只是模板标题, 真正落地的是 created["objects"]
        targets = {r["name"], *created["companions"], *created.get("objects", [])}
        mine = [o for o in ps.com_all_code() if o.get("name") in targets]
        findings = lint_objects(mine, style=r.get("style", "default"))
        created["lint"] = {"summary": summarize(findings), "findings": findings}
    created["status"] = "partial" if created.get("library_warnings") else "added"
    return created


# ---------------------------------------------------------------- 抽取（COM）

def extract_fb(fb_name: str, slug: str, category: str = "",
               description: str = "", companions: list[str] | None = None,
               root: Path | None = None) -> dict:
    """从打开的项目反向抽取一个 FB 成模板（写 fblib/<slug>/）。

    archetype 自动判定；libraries/params 留待人工在 manifest 补全。字面量不自动参数化。
    """
    from . import _ps_bridge as ps
    root = root or fblib_dir()
    info = ps.com_read_pou(fb_name)
    decl = info.get("declaration", "") or ""
    impl = info.get("implementation", "") or ""

    archetype = _detect_archetype(decl)
    d = root / slug
    (d / "types").mkdir(parents=True, exist_ok=True)
    (d / f"{fb_name}.decl").write_text(decl, encoding="utf-8")
    (d / f"{fb_name}.impl").write_text(impl, encoding="utf-8")

    comp_names = companions or []
    manifest = {
        "name": fb_name,
        "slug": slug,
        "category": category or "misc",
        "archetype": archetype,
        "format": "text",
        "description": description or f"从项目抽取的 {fb_name}",
        "version": "1.0",
        "libraries": [],          # 人工补
        "companions": comp_names,
        "methods": [m.get("name") for m in info.get("methods", []) or []],
        "params": [],             # 人工补：把字面量改成 {{param}}
    }
    (d / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return {"slug": slug, "path": str(d), "archetype": archetype,
            "status": "extracted",
            "todo": "编辑 manifest 补 libraries/params，并把 decl/impl 里字面量改成 {{占位符}}"}


def _detect_archetype(decl: str) -> str:
    """判定原型：EXTENDS/IMPLEMENTS→oop；有 bExecute 变量→transaction；否则 cyclic。"""
    if re.search(r"\bEXTENDS\b|\bIMPLEMENTS\b", decl, re.IGNORECASE):
        return "oop"
    from .plc import _parse_variables
    names = {v["name"] for v in _parse_variables(decl)}
    return "transaction" if "bExecute" in names else "cyclic"
