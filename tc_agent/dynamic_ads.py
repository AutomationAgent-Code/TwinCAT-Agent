"""Automatic TwinCAT ADS symbol type inspection and immediate read/write."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


class DynamicAdsError(RuntimeError):
    pass


def bridge_path() -> Path:
    return Path(__file__).resolve().parent / "bin" / "TcAdsDynamicBridge.exe"


def _invoke(net_id: str, port: int, symbol: str, *, depth: int = 3,
            value: object = ...) -> dict:
    executable = bridge_path()
    if not executable.is_file():
        raise DynamicAdsError(f"动态 ADS 桥未安装：{executable}")
    command = [str(executable), str(net_id), str(int(port)), symbol,
               str(max(0, min(int(depth), 8)))]
    if value is not ...:
        command.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DynamicAdsError(f"动态 ADS 桥启动失败：{exc}") from exc
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.strip()), "")
    try:
        result = json.loads(line)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or completed.stdout or "无输出").strip()
        raise DynamicAdsError(f"动态 ADS 桥返回无效结果：{detail}") from exc
    if completed.returncode != 0 or not result.get("ok"):
        raise DynamicAdsError(str(result.get("error") or "动态 ADS 操作失败"))
    return result


def read_symbol(net_id: str, port: int, symbol: str, *, depth: int = 3) -> dict:
    return _invoke(net_id, port, symbol, depth=depth)


def describe_symbol(net_id: str, port: int, symbol: str):
    return _invoke(net_id, port, '@describe:' + symbol, depth=0)


def browse_symbols(net_id: str, port: int, *, query='', parent='', type_filter='', offset=0, limit=40):
    """Metadata-only online discovery; @browse never executes a PLC write."""
    return _invoke(net_id, port, '@browse', depth=0, value={
        'query': query, 'parent': parent, 'type_filter': type_filter,
        'offset': max(0, int(offset)), 'limit': max(1, min(100, int(limit))),
    })


def write_symbol(net_id: str, port: int, symbol: str, value: object,
                 *, depth: int = 3) -> dict:
    return _invoke(net_id, port, symbol, depth=depth, value=value)
