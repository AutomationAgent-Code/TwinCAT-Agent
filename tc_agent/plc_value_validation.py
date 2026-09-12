"""Shared PLC scalar normalization and readback comparison rules.

The ADS writer and the Agent verifier must agree on the value that is actually
stored by TwinCAT.  This module deliberately has no ADS or XAE dependency so
it can be exercised entirely with isolated unit tests.
"""

from __future__ import annotations

import ctypes
import math
import re


class ValueNormalizationError(ValueError):
    """The requested value cannot be represented by the declared PLC type."""


_INTEGER_LAYOUT = {
    "sint": (8, True), "usint": (8, False), "byte": (8, False),
    "int": (16, True), "uint": (16, False), "word": (16, False),
    "dint": (32, True), "udint": (32, False), "dword": (32, False),
    "lint": (64, True), "ulint": (64, False), "lword": (64, False),
    "time": (32, False), "date": (32, False), "tod": (32, False),
    "dt": (32, False), "ltime": (64, False),
}
_FLOAT_KINDS = {"real", "lreal"}
_BOOL_KINDS = {"bool"}
_STRING_RE = re.compile(r"^(?:w?string)(?:\s*\(\s*\d+\s*\))?$", re.I)


def canonical_kind(kind: object) -> str:
    """Normalize a PLC/ADS type name without guessing user-defined types."""
    text = str(kind or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def normalize_tolerance(tolerance: object) -> float | None:
    if tolerance is None:
        return None
    if isinstance(tolerance, bool):
        raise ValueNormalizationError("tolerance 必须是有限的非负数")
    try:
        value = float(tolerance)
    except (TypeError, ValueError) as exc:
        raise ValueNormalizationError("tolerance 必须是有限的非负数") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueNormalizationError("tolerance 必须是有限的非负数")
    return value


def normalize_scalar(kind: object, raw_value: object) -> object:
    """Normalize one value exactly as the scalar ADS write layer does."""
    normalized = canonical_kind(kind)
    if normalized in _BOOL_KINDS:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, int) and raw_value in (0, 1):
            return bool(raw_value)
        if isinstance(raw_value, str):
            text = raw_value.strip().lower()
            if text in {"true", "1"}:
                return True
            if text in {"false", "0"}:
                return False
        raise ValueNormalizationError(
            f"BOOL value must be true/false or 0/1, got {raw_value!r}"
        )

    if normalized in _FLOAT_KINDS:
        if isinstance(raw_value, bool):
            raise ValueNormalizationError(f"{kind} value cannot be boolean")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueNormalizationError(f"Invalid {kind} value: {raw_value!r}") from exc
        if not math.isfinite(value):
            raise ValueNormalizationError(f"{kind} value must be finite")
        # REAL is stored as IEEE-754 single precision.  Round the expected
        # value before comparison so JSON "75.0" and ADS REAL 75 agree.
        return ctypes.c_float(value).value if normalized == "real" else ctypes.c_double(value).value

    if normalized in _INTEGER_LAYOUT:
        if isinstance(raw_value, bool):
            raise ValueNormalizationError(f"{kind} value cannot be boolean")
        if isinstance(raw_value, int):
            value = raw_value
        elif isinstance(raw_value, float):
            if not math.isfinite(raw_value) or not raw_value.is_integer():
                raise ValueNormalizationError(f"Invalid {kind} value: {raw_value!r}")
            value = int(raw_value)
        elif isinstance(raw_value, str) and re.fullmatch(r"[+-]?\d+", raw_value.strip()):
            value = int(raw_value.strip(), 10)
        else:
            raise ValueNormalizationError(f"Invalid {kind} value: {raw_value!r}")
        bits, signed = _INTEGER_LAYOUT[normalized]
        minimum = -(1 << (bits - 1)) if signed else 0
        maximum = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
        if not minimum <= value <= maximum:
            raise ValueNormalizationError(
                f"{kind} value {value} outside {minimum}..{maximum}"
            )
        return value

    if _STRING_RE.fullmatch(str(kind or "").strip()):
        if not isinstance(raw_value, str):
            raise ValueNormalizationError(f"{kind} value must be a string")
        return raw_value

    raise ValueNormalizationError(f"Unsupported PLC scalar type: {kind}")


def default_float_tolerance(kind: object, expected: object) -> float:
    """Allow a small, type-specific number of storage ULPs by default."""
    normalized = canonical_kind(kind)
    value = abs(float(expected))
    epsilon = 2.0 ** (-23 if normalized == "real" else -52)
    return 8.0 * epsilon * max(1.0, value)


def scalar_values_match(actual: object, expected: object, kind: object,
                        tolerance: object = None) -> tuple[bool, object]:
    """Return ``(matched, normalized_expected)`` or raise on invalid input."""
    normalized_kind = canonical_kind(kind)
    normalized_expected = normalize_scalar(normalized_kind, expected)
    normalized_actual = normalize_scalar(normalized_kind, actual)
    if normalized_kind in _FLOAT_KINDS:
        limit = normalize_tolerance(tolerance)
        if limit is None:
            limit = default_float_tolerance(normalized_kind, normalized_expected)
        return abs(float(normalized_actual) - float(normalized_expected)) <= limit, normalized_expected
    # Strings remain strings and integers remain integers; no float equality is
    # used here, so LINT/ULINT values cannot lose precision.
    return normalized_actual == normalized_expected, normalized_expected


def is_known_scalar_kind(kind: object) -> bool:
    text = str(kind or "").strip()
    return canonical_kind(kind) in _BOOL_KINDS | _FLOAT_KINDS | set(_INTEGER_LAYOUT) or bool(_STRING_RE.fullmatch(text))
