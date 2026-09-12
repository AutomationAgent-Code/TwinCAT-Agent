"""Small ctypes ADS helpers used by the product runtime.

This deliberately uses the installed Beckhoff TcAdsDll instead of pyads so the
portable customer package does not need another Python dependency.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from tc_agent.plc_value_validation import (
    ValueNormalizationError, normalize_scalar,
)


class AdsStateError(RuntimeError):
    pass


class _AmsNetId(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("b", ctypes.c_ubyte * 6)]


class _AmsAddr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("net_id", _AmsNetId), ("port", ctypes.c_ushort)]


ADS_STATE_NAMES = {
    0: "Invalid",
    1: "Idle",
    2: "Reset",
    3: "Init",
    4: "Start",
    5: "Run",
    6: "Stop",
    7: "Config",
    8: "Reconfig (Config requested)",
    15: "Config/CP-Panel",
}

# Values from TcAdsDef.h / ADSSTATE. ``RECONFIG`` requests that the System
# Service restart TwinCAT in Config mode when sent to ADS port 10000.
ADSSTATE_RECONFIG = 8
ADSSTATE_RESET = 2
ADS_SYSTEM_SERVICE_PORT = 10000


def _parse_net_id(value: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in value.strip().split("."))
    except ValueError as exc:
        raise AdsStateError(f"无效 AMS NetId：{value}") from exc
    if len(parts) != 6 or any(part < 0 or part > 255 for part in parts):
        raise AdsStateError(f"无效 AMS NetId：{value}")
    return parts


def _ads_dll() -> Path:
    common = "Common64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Common32"
    override = os.environ.get("TC_AGENT_ADS_DLL", "").strip()
    candidates = [
        Path(override) if override else None,
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Beckhoff" / "TwinCAT" / common / "TcAdsDll.dll",
        Path(r"C:\TwinCAT\AdsApi\TcAdsDll")
        / ("x64" if common == "Common64" else "")
        / "TcAdsDll.dll",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise AdsStateError("未找到 Beckhoff TcAdsDll.dll")


def read_ads_state(net_id: str, port: int = 300) -> dict:
    """Read ADS/device state from the target's System Service (port 300)."""
    parts = _parse_net_id(net_id)
    try:
        dll = ctypes.WinDLL(str(_ads_dll()))
        dll.AdsPortOpenEx.argtypes = []
        dll.AdsPortOpenEx.restype = ctypes.c_long
        dll.AdsPortCloseEx.argtypes = [ctypes.c_long]
        dll.AdsPortCloseEx.restype = ctypes.c_long
        dll.AdsSyncReadStateReqEx.argtypes = [
            ctypes.c_long,
            ctypes.POINTER(_AmsAddr),
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        dll.AdsSyncReadStateReqEx.restype = ctypes.c_long
    except Exception as exc:
        raise AdsStateError(f"加载 ADS 状态接口失败：{exc}") from exc

    client_port = dll.AdsPortOpenEx()
    if client_port <= 0:
        raise AdsStateError("无法连接本机 TwinCAT ADS Router")
    try:
        address = _AmsAddr()
        for index, value in enumerate(parts):
            address.net_id.b[index] = value
        address.port = int(port)
        ads_state = ctypes.c_ushort()
        device_state = ctypes.c_ushort()
        result = dll.AdsSyncReadStateReqEx(
            client_port,
            ctypes.byref(address),
            ctypes.byref(ads_state),
            ctypes.byref(device_state),
        )
        if result:
            raise AdsStateError(
                f"读取 {net_id}:{port} 状态失败（ADS {result}）")
        code = int(ads_state.value)
        return {
            "net_id": net_id,
            "port": int(port),
            "state_code": code,
            "state_name": ADS_STATE_NAMES.get(code, f"Unknown({code})"),
            "device_state": int(device_state.value),
            "is_config": code in (7, 8, 15),
        }
    finally:
        dll.AdsPortCloseEx(client_port)


def write_ads_control(net_id: str, port: int, ads_state: int,
                      device_state: int = 0) -> dict:
    """Request an ADS device-state transition without XAE UI interaction.

    The caller must verify the asynchronous transition independently.
    """
    parts = _parse_net_id(net_id)
    try:
        dll = ctypes.WinDLL(str(_ads_dll()))
        dll.AdsPortOpenEx.argtypes = []
        dll.AdsPortOpenEx.restype = ctypes.c_long
        dll.AdsPortCloseEx.argtypes = [ctypes.c_long]
        dll.AdsPortCloseEx.restype = ctypes.c_long
        dll.AdsSyncWriteControlReqEx.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_ushort,
            ctypes.c_ushort, ctypes.c_uint32, ctypes.c_void_p,
        ]
        dll.AdsSyncWriteControlReqEx.restype = ctypes.c_long
    except Exception as exc:
        raise AdsStateError(f"加载 ADS 状态切换接口失败：{exc}") from exc

    client_port = dll.AdsPortOpenEx()
    if client_port <= 0:
        raise AdsStateError("无法连接本机 TwinCAT ADS Router")
    try:
        address = _AmsAddr()
        for index, value in enumerate(parts):
            address.net_id.b[index] = value
        address.port = int(port)
        result = dll.AdsSyncWriteControlReqEx(
            client_port, ctypes.byref(address), int(ads_state),
            int(device_state), 0, None,
        )
        if result:
            raise AdsStateError(
                f"切换 {net_id}:{port} 状态失败（ADS {result}）")
        return {
            "net_id": net_id,
            "port": int(port),
            "requested_state": int(ads_state),
            "requested_name": ADS_STATE_NAMES.get(int(ads_state), str(ads_state)),
        }
    finally:
        dll.AdsPortCloseEx(client_port)


_ADS_TYPES = {
    "bool": ctypes.c_ubyte,
    "sint": ctypes.c_int8,
    "usint": ctypes.c_uint8,
    "byte": ctypes.c_ubyte,
    "int": ctypes.c_int16,
    "uint": ctypes.c_ushort,
    "word": ctypes.c_ushort,
    "dint": ctypes.c_int32,
    "udint": ctypes.c_uint32,
    "dword": ctypes.c_uint32,
    "lint": ctypes.c_int64,
    "ulint": ctypes.c_uint64,
    "lword": ctypes.c_uint64,
    "real": ctypes.c_float,
    "lreal": ctypes.c_double,
    "time": ctypes.c_uint32,
    "ltime": ctypes.c_uint64,
    "date": ctypes.c_uint32,
    "tod": ctypes.c_uint32,
    "dt": ctypes.c_uint32,
}


def _coerce_ads_scalar(kind: str, raw_value: object) -> object:
    """Convert JSON values without allowing ctypes integer wraparound."""
    normalized = str(kind or "").strip().lower()
    if normalized not in _ADS_TYPES:
        raise AdsStateError(f"Unsupported ADS scalar type: {kind}")
    try:
        value = normalize_scalar(normalized, raw_value)
    except ValueNormalizationError as exc:
        raise AdsStateError(str(exc)) from exc
    return int(value) if normalized == "bool" else value


def read_ads_values_by_name(net_id: str, port: int,
                            symbols: dict[str, str]) -> dict[str, object]:
    """Read scalar PLC symbols through TcAdsDll without a pyads dependency."""
    parts = _parse_net_id(net_id)
    if not symbols:
        return {}
    unknown = sorted({kind.lower() for kind in symbols.values()} - set(_ADS_TYPES))
    if unknown:
        raise AdsStateError("Unsupported ADS scalar type(s): " + ", ".join(unknown))
    try:
        dll = ctypes.WinDLL(str(_ads_dll()))
        dll.AdsPortOpenEx.restype = ctypes.c_long
        dll.AdsPortCloseEx.argtypes = [ctypes.c_long]
        dll.AdsSyncReadWriteReqEx2.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.AdsSyncReadWriteReqEx2.restype = ctypes.c_long
        dll.AdsSyncReadReqEx2.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.AdsSyncReadReqEx2.restype = ctypes.c_long
        dll.AdsSyncWriteReqEx.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        dll.AdsSyncWriteReqEx.restype = ctypes.c_long
    except Exception as exc:
        raise AdsStateError(f"Failed to load ADS symbol API: {exc}") from exc

    client_port = dll.AdsPortOpenEx()
    if client_port <= 0:
        raise AdsStateError("Cannot connect to the local TwinCAT ADS Router")
    address = _AmsAddr()
    for index, value in enumerate(parts):
        address.net_id.b[index] = value
    address.port = int(port)
    results: dict[str, object] = {}
    try:
        for symbol, kind in symbols.items():
            encoded = symbol.encode("utf-8") + b"\x00"
            name_buffer = ctypes.create_string_buffer(encoded)
            handle = ctypes.c_uint32()
            bytes_read = ctypes.c_uint32()
            error = dll.AdsSyncReadWriteReqEx2(
                client_port, ctypes.byref(address), 0xF003, 0,
                ctypes.sizeof(handle), ctypes.byref(handle), len(encoded),
                ctypes.byref(name_buffer), ctypes.byref(bytes_read),
            )
            if error:
                raise AdsStateError(
                    f"ADS symbol handle failed for {symbol} ({net_id}:{port}, ADS {error})"
                )
            try:
                value_type = _ADS_TYPES[kind.lower()]
                value = value_type()
                bytes_read = ctypes.c_uint32()
                error = dll.AdsSyncReadReqEx2(
                    client_port, ctypes.byref(address), 0xF005, handle.value,
                    ctypes.sizeof(value), ctypes.byref(value), ctypes.byref(bytes_read),
                )
                if error:
                    raise AdsStateError(
                        f"ADS symbol read failed for {symbol} ({net_id}:{port}, ADS {error})"
                    )
                results[symbol] = bool(value.value) if kind.lower() == "bool" else value.value
            finally:
                dll.AdsSyncWriteReqEx(
                    client_port, ctypes.byref(address), 0xF006, 0,
                    ctypes.sizeof(handle), ctypes.byref(handle),
                )
        return results
    finally:
        dll.AdsPortCloseEx(client_port)


def write_ads_values_by_name(net_id: str, port: int,
                             values: dict[str, tuple[str, object]]) -> dict[str, object]:
    """Write scalar PLC symbols through TcAdsDll and report written values."""
    parts = _parse_net_id(net_id)
    if not values:
        return {}
    try:
        dll = ctypes.WinDLL(str(_ads_dll()))
        dll.AdsPortOpenEx.restype = ctypes.c_long
        dll.AdsPortCloseEx.argtypes = [ctypes.c_long]
        dll.AdsSyncReadWriteReqEx2.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.AdsSyncReadWriteReqEx2.restype = ctypes.c_long
        dll.AdsSyncWriteReqEx.argtypes = [
            ctypes.c_long, ctypes.POINTER(_AmsAddr), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        dll.AdsSyncWriteReqEx.restype = ctypes.c_long
    except Exception as exc:
        raise AdsStateError(f"Failed to load ADS symbol API: {exc}") from exc

    client_port = dll.AdsPortOpenEx()
    if client_port <= 0:
        raise AdsStateError("Cannot connect to the local TwinCAT ADS Router")
    address = _AmsAddr()
    for index, value in enumerate(parts):
        address.net_id.b[index] = value
    address.port = int(port)
    written = {}
    try:
        for symbol, typed_value in values.items():
            kind, raw_value = typed_value
            value_type = _ADS_TYPES.get(str(kind).lower())
            if value_type is None:
                raise AdsStateError(f"Unsupported ADS scalar type: {kind}")
            encoded = symbol.encode("utf-8") + b"\x00"
            name_buffer = ctypes.create_string_buffer(encoded)
            handle = ctypes.c_uint32()
            bytes_read = ctypes.c_uint32()
            error = dll.AdsSyncReadWriteReqEx2(
                client_port, ctypes.byref(address), 0xF003, 0,
                ctypes.sizeof(handle), ctypes.byref(handle), len(encoded),
                ctypes.byref(name_buffer), ctypes.byref(bytes_read),
            )
            if error:
                raise AdsStateError(f"ADS symbol handle failed for {symbol} (ADS {error})")
            try:
                data = value_type(_coerce_ads_scalar(str(kind), raw_value))
                error = dll.AdsSyncWriteReqEx(
                    client_port, ctypes.byref(address), 0xF005, handle.value,
                    ctypes.sizeof(data), ctypes.byref(data),
                )
                if error:
                    raise AdsStateError(f"ADS symbol write failed for {symbol} (ADS {error})")
                written[symbol] = bool(data.value) if str(kind).lower() == "bool" else data.value
            finally:
                dll.AdsSyncWriteReqEx(
                    client_port, ctypes.byref(address), 0xF006, 0,
                    ctypes.sizeof(handle), ctypes.byref(handle),
                )
        return written
    finally:
        dll.AdsPortCloseEx(client_port)
