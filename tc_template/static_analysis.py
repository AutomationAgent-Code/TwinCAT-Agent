"""Offline PLC Structured Text static analysis.

``TCSA`` (TwinCAT Agent Static Analysis) is intentionally a small, transparent
rule engine.  It is useful where TE1200 is not installed, but it does *not*
execute TE1200 rules and must never be reported as a TE1200 result.

The input has the same shape as :func:`tc_template._ps_bridge.com_all_code`,
which makes the analyser usable both against an open XAE project and in unit
tests without TwinCAT or a Windows COM dependency.
"""

from __future__ import annotations

import re
from decimal import Decimal
from collections import Counter
from typing import Iterable

from .plc import _parse_variables


ENGINE_NAME = "TwinCAT Agent Static Analysis"
ENGINE_ID = "TCSA"
TE1200_DISCLAIMER = (
    "TCSA is an offline heuristic analysis. TE1200 was not executed; this "
    "result cannot prove that TE1200 rules pass."
)

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_CONTROL_START = re.compile(r"^\s*(IF\b|FOR\b|WHILE\b|REPEAT\b|CASE\b)", re.I)
_CONTROL_END = re.compile(r"^\s*END_(IF|FOR|WHILE|REPEAT|CASE)\b", re.I)
_ELSE_IF = re.compile(r"^\s*ELSIF\b", re.I)
_COMMENTED_CODE = re.compile(
    r"^\s*//\s*(?:IF\b|ELSIF\b|ELSE\b|CASE\b|FOR\b|WHILE\b|REPEAT\b|"
    r"END_(?:IF|CASE|FOR|WHILE|REPEAT)\b|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?\s*:=)",
    re.I,
)


def _finding(rule: str, severity: str, obj: str, area: str, line: int,
             message: str, *, confidence: str = "high",
             inspired_by: str = "") -> dict:
    item = {
        "rule": rule,
        "severity": severity,
        "object": obj,
        "area": area,
        "line": line,
        "column": 1,
        "message": message,
        "confidence": confidence,
    }
    if inspired_by:
        item["inspired_by"] = inspired_by
    return item


def _without_comments(text: str) -> str:
    """Mask strings and nested comments, preserving source offsets."""
    from .lint import _st_code
    return _st_code(text)


_NUMBER = r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
_ZERO_DIVISOR = re.compile(
    rf'(?:/|\bMOD\b)\s*(?:\(\s*(?P<wrapped>{_NUMBER})\s*\)|'
    rf'(?P<plain>{_NUMBER})(?![\w.#]))', re.I)
_STATEFUL_TYPES = {'TON', 'TOF', 'TP', 'R_TRIG', 'F_TRIG', 'CTU', 'CTD', 'CTUD'}


def _case_without_else(code: str):
    """Match nested statement blocks so an inner ELSE cannot satisfy a CASE."""
    stack = []
    for token in re.finditer(r'\b(?:CASE|IF|FOR|WHILE|REPEAT|ELSE|END_CASE|END_IF|END_FOR|END_WHILE|END_REPEAT)\b', code, re.I):
        word = token.group().upper()
        if word in {'CASE', 'IF', 'FOR', 'WHILE', 'REPEAT'}:
            stack.append([word, token.start(), False])
        elif word == 'ELSE':
            if stack:
                stack[-1][2] = True
        elif stack and word == 'END_' + stack[-1][0]:
            kind, offset, has_else = stack.pop()
            if kind == 'CASE' and not has_else:
                yield code.count('\n', 0, offset) + 1


def _method_areas(obj: dict) -> Iterable[tuple[str, str]]:
    implementation = str(obj.get("implementation") or "")
    if implementation:
        yield "implementation", implementation
    for member in obj.get("methods") or []:
        if not isinstance(member, dict):
            continue
        text = str(member.get("implementation") or member.get("code") or "")
        if text:
            label = str(member.get("name") or member.get("path") or "member")
            yield f"member:{label}", text


def _complexity(text: str) -> int:
    """Compatibility entry point for the documented ST-subset metric."""
    from .static_rules import cognitive_complexity
    return cognitive_complexity(text)


def _occurrences(text: str, name: str) -> int:
    return len(re.findall(rf"\b{re.escape(name)}\b", _without_comments(text), re.I))


def _parenthesis_depths(text: str) -> list[int]:
    """Return nesting depth before each character, ignoring ST string text.

    Named FB/function arguments use ``port := value`` inside ``(...)``.  Such
    connections are not assignments to a same-named VAR_INPUT of the current
    POU and must be excluded from TCSA0037.
    """
    depths = [0] * len(text)
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        depths[index] = depth
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    depths[index + 1] = depth
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        index += 1
    return depths


def analyze_objects(objects: list[dict], max_complexity: int = 20,
                    rule_severities: dict | None = None, *, project_wide: bool = True) -> dict:
    """Analyse PLC objects and return TCSA findings plus explicit limitations.

    Uses lexical checks, supplied-source references and bounded scalar intervals.
    Cross-task scheduling, pointer lifetime, full type inference and indirect
    calls require compiler information and are not claimed as verified.
    """
    if max_complexity < 1:
        raise ValueError("max_complexity must be at least 1")
    policy = dict(rule_severities or {})
    for key, value in policy.items():
        if not re.fullmatch(r'SA\d{4}', key) or value not in {'off', 'warning', 'error'}:
            raise ValueError('rule_severities requires SAxxxx: off/warning/error')

    findings: list[dict] = []
    for obj in objects:
        name = str(obj.get("name") or "<unnamed>")
        declaration = str(obj.get("declaration") or "")
        variables = _parse_variables(_without_comments(declaration))
        areas = list(_method_areas(obj))
        all_code = "\n".join(text for _, text in areas)

        input_names = {v["name"] for v in variables if v.get("scope") == "input"}
        real_names = set()
        for variable in variables:
            type_tokens = str(variable.get("type") or "").upper().split()
            if type_tokens and type_tokens[0] in {"REAL", "LREAL"}:
                real_names.add(variable["name"])

        # TCSA0167 — FB instances in VAR_TEMP are recreated every call.
        for variable in variables:
            var_type = str(variable.get("type") or "").upper().lstrip()
            if variable.get("scope") == "temp" and (var_type.startswith("FB_") or var_type in _STATEFUL_TYPES):
                findings.append(_finding(
                    "TCSA0167", "warning", name, "declaration", 1,
                    f"Function block instance '{variable['name']}' is declared in VAR_TEMP.",
                    confidence="high", inspired_by="SA0167",
                ))

        for area, text in areas:
            clean = _without_comments(text)
            parenthesis_depths = _parenthesis_depths(clean)
            for line_no, raw in enumerate(text.splitlines(), start=1):
                if _COMMENTED_CODE.match(raw):
                    findings.append(_finding(
                        "TCSA0140", "warning", name, area, line_no,
                        "Commented-out Structured Text statement should be removed or restored.",
                        confidence="high", inspired_by="SA0140",
                    ))

            # Members may shadow parent variables with their own declarations.
            member = next((m for m in obj.get('methods') or []
                           if isinstance(m, dict) and area == 'member:' + str(m.get('name') or m.get('path') or 'member')), None)
            scoped_inputs = set(input_names)
            scoped_reals = set(real_names)
            if member is not None:
                local_vars = _parse_variables(_without_comments(str(member.get('declaration') or '')))
                shadowed = {v['name'].casefold() for v in local_vars}
                scoped_inputs = {n for n in scoped_inputs if n.casefold() not in shadowed}
                scoped_reals = {n for n in scoped_reals if n.casefold() not in shadowed}
                scoped_inputs.update(v['name'] for v in local_vars if v.get('scope') == 'input')
                scoped_reals.update(v['name'] for v in local_vars if v.get('type', '').upper() in {'REAL', 'LREAL'})
            for input_name in scoped_inputs:
                assignment = re.compile(
                    rf"(?<![\w.]){re.escape(input_name)}(?:\s*\[[^\]]+\])?\s*:=", re.I)
                for match in assignment.finditer(clean):
                    if parenthesis_depths[match.start()] > 0:
                        continue
                    line_no = clean.count("\n", 0, match.start()) + 1
                    findings.append(_finding(
                        "TCSA0037", "error", name, area, line_no,
                        f"Input variable '{input_name}' is assigned in its implementation.",
                        confidence="high", inspired_by="SA0037",
                    ))

            for match in _ZERO_DIVISOR.finditer(clean):
                if Decimal(match.group('wrapped') or match.group('plain')) != 0:
                    continue
                line_no = clean.count("\n", 0, match.start()) + 1
                findings.append(_finding(
                    "TCSA0040", "error", name, area, line_no,
                    "Division or MOD by literal zero.",
                    confidence="high", inspired_by="SA0040",
                ))

            for line_no in _case_without_else(clean):
                findings.append(_finding(
                    'TCSA0075', 'warning', name, area, line_no,
                    'CASE has no ELSE branch; define invalid-state handling.',
                    inspired_by='SA0075'))

            for real_name in scoped_reals:
                # Assignment is ':='; an unadorned '=' or '<>' beside a REAL
                # is a direct equality comparison. Relational < and > are fine.
                left = re.compile(rf"\b{re.escape(real_name)}\b\s*(?:<>|(?<![:<>=])=(?!=))", re.I)
                right = re.compile(rf"(?:<>|(?<![:<>=])=(?!=))\s*\b{re.escape(real_name)}\b", re.I)
                seen_lines: set[int] = set()
                for match in list(left.finditer(clean)) + list(right.finditer(clean)):
                    line_no = clean.count("\n", 0, match.start()) + 1
                    if line_no in seen_lines:
                        continue
                    seen_lines.add(line_no)
                    findings.append(_finding(
                        "TCSA0054", "warning", name, area, line_no,
                        f"Direct equality comparison involving REAL/LREAL '{real_name}'. Use a tolerance.",
                        confidence="high", inspired_by="SA0054",
                    ))

            score = _complexity(text)
            if score > max_complexity:
                findings.append(_finding(
                    "TCSA0178", "warning", name, area, 1,
                    f"ST subset cognitive complexity is {score}; configured limit is {max_complexity} (not a compiler-equivalent metric).",
                    confidence="medium", inspired_by="SA0178",
                ))

    from .static_rules import semantic_checks
    findings.extend(semantic_checks(objects, project_wide=project_wide))
    findings = [f for f in findings if policy.get(f.get('inspired_by')) != 'off']
    for f in findings:
        f['severity'] = policy.get(f.get('inspired_by'), f['severity'])
    findings.sort(key=lambda f: (
        _SEVERITY_ORDER.get(f["severity"], 9), f["object"], f["area"], f["line"], f["rule"]
    ))
    counts = Counter(f["severity"] for f in findings)
    by_rule = Counter(f["rule"] for f in findings)
    return {
        "engine": ENGINE_ID,
        "engine_name": ENGINE_NAME,
        "te1200_executed": False,
        "te1200_status": "not-executed",
        "license_required": False,
        "configuration": {"max_complexity": max_complexity, "rule_severities": policy,
                          "source_scope": "supplied_project_objects" if project_wide else "write_candidate"},
        "coverage": {
            "SA0027": "supplied enum constants, qualified_only exemption; libraries not resolved",
            "SA0033": "supplied source variable references; external consumers not resolved",
            "SA0038": "own output reads including members; indirect access not resolved",
            "SA0043": "supplied global/POU references; HMI and ADS consumers require review",
            "SA0040": "literal zero and supported scalar expression intervals",
            "SA0062": "supported scalar conditions and limited branch propagation",
            "SA0172": "literal one-dimensional array bounds and supported index intervals",
            "SA0178": "token-based ST subset; not compiler-equivalent",
            "C0555": "not checked; compiler string encoding setting required",
        },
        "disclaimer": TE1200_DISCLAIMER,
        "summary": {
            "total": len(findings),
            "errors": counts["error"],
            "warnings": counts["warning"],
            "info": counts["info"],
            "by_rule": dict(sorted(by_rule.items())),
        },
        "findings": findings,
        "limitations": [
            "Limited source intervals only: no complete type-flow, alias, pointer/reference, library or array-bound proof.",
            "No task scheduling model: multi-task output writes and data races are not detected.",
            "XAE rule settings and general analysis-suppression pragmas are not imported; only explicit per-run policy applies.",
            "No TE1200 execution or rule compatibility guarantee.",
        ],
    }
