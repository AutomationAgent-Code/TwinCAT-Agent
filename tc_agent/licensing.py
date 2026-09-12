"""TwinCAT Agent 离线设备授权。

客户机只包含 RSA 公钥，只能验证授权码；签发授权码的私钥保存在供应商电脑，
绝不能进入仓库或安装包。设备码直接使用 TwinCAT License Server 提供的 System ID；
当本机 ADS License Server 暂未运行时，回退读取已有 TwinCAT 许可证文件。授权码格式：

    TCAG1.<base64url(JSON payload)>.<base64url(RSA-SHA256 signature)>

payload 与 TwinCAT System ID 绑定，因此复制到另一台电脑无效。
"""

from __future__ import annotations

import base64
import ctypes
import functools
import hashlib
import hmac
import json
import os
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

PRODUCT_ID = "twincat-agent"
CODE_PREFIX = "TCAG1"
PUBLIC_EXPONENT = 65537
PUBLIC_MODULUS = int(
    "a333cc74a02b50542f4569dc9af36df9e60e7253cabb86887596f5b63fdad1dc"
    "96b695ac0698dca849a9b2c4ae563e9c729084c4806b44ae0dcf614a3e2c2bad"
    "804bb2ca0476f36512396f455ae278a6bd9961081202aa3494ecd0dd45bf583315"
    "636a495f8a0cf8a7312f58f344d04987def279c3b995189f3512cdbb7df71b701"
    "78d3f0539caebabd8b569a15d099dee093776aaac61ebc357c6eefb11b0232ac45"
    "1bfbae16de282b22296a054425b29e124baaa4b76e79f6eaa519f27dbb7dcda871"
    "e8037cfe32b39f0cc7545bbca4a6c92605786024185864f83a88c3b415b0837c0"
    "874263add7f73de66f82c54885f6c75e4dddd86ee47c147d8cf032317655f07c4b"
    "18d751f5621b30469174090b855ddfd89693f74bb94775e7aab017fdf9c2eca098"
    "4006bc761db200b5b146a110740c240bc3f5f3a09df3bdc9f5b4f6c46d2949869"
    "76ae266c2fb1c82bf502b1cc82c0eccb5b7d4db6216fc54ad1d74636f9387b40b"
    "5a1f3076c7a596b65f263094af05779922e8b0c8c4ef96bd43",
    16,
)

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_LOCK = threading.Lock()


def _license_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "TwinCAT Agent" / "license.json"
    return Path.home() / ".twincat-agent" / "license.json"


class DeviceFingerprintError(RuntimeError):
    """无法从 TwinCAT 读取有效的 System ID。"""


_AMS_PORT_LICENSE_SERVER = 30
_IGRP_LIC_SYSTEM_INFO = 0x01010004
_IOFFS_LIC_SYSTEM_ID = 1
_ADS_SYSTEM_ID_TIMEOUT_MS = 1500


class _AmsNetId(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("b", ctypes.c_ubyte * 6)]


class _AmsAddr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("net_id", _AmsNetId), ("port", ctypes.c_ushort)]


def _normalize_system_id(value: str | uuid.UUID) -> str:
    try:
        parsed = (
            value
            if isinstance(value, uuid.UUID)
            else uuid.UUID(str(value).strip("{} "))
        )
    except (ValueError, AttributeError) as exc:
        raise DeviceFingerprintError("TwinCAT System ID 格式无效") from exc
    if parsed.int == 0:
        raise DeviceFingerprintError("TwinCAT System ID 为空")
    return str(parsed).upper()


def _ads_dll_candidates() -> list[Path]:
    override = os.environ.get("TC_AGENT_ADS_DLL", "").strip()
    common = "Common64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Common32"
    legacy_arch = "x64" if common == "Common64" else ""
    roots = _twincat_roots()
    candidates = [
        Path(override) if override else None,
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Beckhoff" / "TwinCAT" / common / "TcAdsDll.dll",
        *[root.parent / common / "TcAdsDll.dll" for root in roots],
        *[root.parent / "AdsApi" / "TcAdsDll" / legacy_arch / "TcAdsDll.dll"
          for root in roots],
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / ("System32" if common == "Common64" else "SysWOW64")
        / "TcAdsDll.dll",
    ]
    result: list[Path] = []
    for path in candidates:
        if path and path.is_file() and path not in result:
            result.append(path)
    return result


def _twincat_roots() -> list[Path]:
    candidates = [
        Path(os.environ["TWINCAT3DIR"]) if os.environ.get("TWINCAT3DIR") else None,
        Path(r"C:\TwinCAT\3.1"),
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Beckhoff" / "TwinCAT" / "3.1",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Beckhoff" / "TwinCAT" / "3.1",
    ]
    result: list[Path] = []
    for root in candidates:
        if root and root not in result:
            result.append(root)
    return result


def _read_system_id_ads() -> str:
    """直接向本机 TwinCAT ADS License Server 读取 16 字节 System ID。"""
    candidates = _ads_dll_candidates()
    if not candidates:
        raise DeviceFingerprintError("未找到 TwinCAT ADS 组件")

    try:
        dll = ctypes.WinDLL(str(candidates[0]))
        dll.AdsPortOpenEx.argtypes = []
        dll.AdsPortOpenEx.restype = ctypes.c_long
        dll.AdsPortCloseEx.argtypes = [ctypes.c_long]
        dll.AdsPortCloseEx.restype = ctypes.c_long
        dll.AdsSyncSetTimeoutEx.argtypes = [ctypes.c_long, ctypes.c_uint32]
        dll.AdsSyncSetTimeoutEx.restype = ctypes.c_long
        dll.AdsGetLocalAddressEx.argtypes = [
            ctypes.c_long,
            ctypes.POINTER(_AmsAddr),
        ]
        dll.AdsGetLocalAddressEx.restype = ctypes.c_long
        dll.AdsSyncReadReqEx2.argtypes = [
            ctypes.c_long,
            ctypes.POINTER(_AmsAddr),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.AdsSyncReadReqEx2.restype = ctypes.c_long
    except Exception as exc:
        raise DeviceFingerprintError(f"加载 TwinCAT ADS 组件失败：{exc}") from exc

    client_port = dll.AdsPortOpenEx()
    if client_port <= 0:
        raise DeviceFingerprintError("无法连接 TwinCAT ADS Router")
    try:
        timeout_result = dll.AdsSyncSetTimeoutEx(
            client_port, _ADS_SYSTEM_ID_TIMEOUT_MS
        )
        if timeout_result:
            raise DeviceFingerprintError(
                f"设置 TwinCAT ADS 读取超时失败（ADS {timeout_result}）"
            )
        address = _AmsAddr()
        result = dll.AdsGetLocalAddressEx(client_port, ctypes.byref(address))
        if result:
            raise DeviceFingerprintError(f"读取本机 AMS 地址失败（ADS {result}）")
        address.port = _AMS_PORT_LICENSE_SERVER
        raw = (ctypes.c_ubyte * 16)()
        returned = ctypes.c_uint32()
        result = dll.AdsSyncReadReqEx2(
            client_port,
            ctypes.byref(address),
            _IGRP_LIC_SYSTEM_INFO,
            _IOFFS_LIC_SYSTEM_ID,
            16,
            raw,
            ctypes.byref(returned),
        )
        if result or returned.value != 16:
            raise DeviceFingerprintError(
                f"读取 TwinCAT System ID 失败"
                f"（ADS {result}，返回 {returned.value} 字节）"
            )
        # Windows GUID 的前三段在内存中使用 little-endian。
        return _normalize_system_id(uuid.UUID(bytes_le=bytes(raw)))
    finally:
        dll.AdsPortCloseEx(client_port)


def _license_file_candidates() -> list[Path]:
    roots = []
    for twincat_root in _twincat_roots():
        roots.extend((
            twincat_root / "Target" / "License",
            twincat_root / "License",
        ))
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(root.glob("*.tclrs"))
            files.extend(root.glob("*.tclrq"))
    return sorted(
        set(files),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )


def _read_system_id_license_file() -> str:
    """ADS 暂不可用时，从最近生成的许可证文件读取 System ID。"""
    for path in _license_file_candidates():
        try:
            root = ET.parse(path).getroot()
            node = next(
                (item for item in root.iter()
                 if item.tag.rsplit("}", 1)[-1].lower() == "systemid"),
                None,
            )
            if node is not None and node.text:
                return _normalize_system_id(node.text)
        except (OSError, ET.ParseError, DeviceFingerprintError):
            continue
    raise DeviceFingerprintError("未找到包含 System ID 的 TwinCAT 许可证文件")


@functools.lru_cache(maxsize=1)
def device_id() -> str:
    errors: list[str] = []
    for reader in (_read_system_id_ads, _read_system_id_license_file):
        try:
            return reader()
        except DeviceFingerprintError as exc:
            errors.append(str(exc))
    raise DeviceFingerprintError(
        "无法读取 TwinCAT System ID。请确认 TwinCAT 已安装并启动一次；"
        + " / ".join(errors)
    )


def _b64u_decode(value: str) -> bytes:
    if not value or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in value):
        raise ValueError("授权码编码无效")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_signature(payload: bytes, signature: bytes) -> bool:
    key_bytes = (PUBLIC_MODULUS.bit_length() + 7) // 8
    if len(signature) != key_bytes:
        return False
    recovered = pow(
        int.from_bytes(signature, "big"), PUBLIC_EXPONENT, PUBLIC_MODULUS
    ).to_bytes(key_bytes, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    expected = (
        b"\x00\x01"
        + b"\xff" * (key_bytes - len(digest_info) - 3)
        + b"\x00"
        + digest_info
    )
    return hmac.compare_digest(recovered, expected)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def verify(code: str, now: datetime | None = None) -> dict:
    code = "".join((code or "").split())
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != CODE_PREFIX:
        raise ValueError("授权码格式不正确")
    payload_raw = _b64u_decode(parts[1])
    signature = _b64u_decode(parts[2])
    if not _verify_signature(payload_raw, signature):
        raise ValueError("授权码签名无效或内容已被修改")
    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("授权码内容无效") from exc
    if payload.get("product") != PRODUCT_ID:
        raise ValueError("授权码不属于 TwinCAT Agent")
    if payload.get("device_id") != device_id():
        raise ValueError("授权码与当前设备不匹配")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = payload.get("expires_at")
    if expires and now > _parse_time(expires):
        raise ValueError(f"授权已于 {expires[:10]} 到期")
    return payload


def _public_result(valid: bool, message: str = "", payload: dict | None = None) -> dict:
    payload = payload or {}
    try:
        current_device = device_id()
    except DeviceFingerprintError as exc:
        return {
            "valid": False,
            "message": str(exc),
            "device_id": "",
            "customer": "",
            "license_id": "",
            "expires_at": "",
        }
    return {
        "valid": valid,
        "message": message,
        "device_id": current_device,
        "customer": payload.get("customer", ""),
        "license_id": payload.get("license_id", ""),
        "expires_at": payload.get("expires_at", ""),
    }


def status() -> dict:
    path = _license_path()
    with _LOCK:
        if not path.exists():
            return _public_result(False, "请输入与本机设备码匹配的授权码")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            payload = verify(record.get("code", ""))
            now = datetime.now(timezone.utc)
            last_seen = record.get("last_seen")
            if last_seen and now.timestamp() + 86400 < _parse_time(last_seen).timestamp():
                return _public_result(False, "检测到系统时间异常，请校准时间后重试")
            record["last_seen"] = now.isoformat().replace("+00:00", "Z")
            _write_record(path, record)
            return _public_result(True, "授权有效", payload)
        except Exception as exc:
            return _public_result(False, str(exc))


def activate(code: str) -> dict:
    with _LOCK:
        try:
            clean = "".join((code or "").split())
            payload = verify(clean)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            path = _license_path()
            _write_record(path, {"code": clean, "activated_at": now, "last_seen": now})
            return _public_result(True, "授权成功", payload)
        except Exception as exc:
            return _public_result(False, str(exc))


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
