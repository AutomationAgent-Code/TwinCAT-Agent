r"""供应商侧 TwinCAT Agent 授权码签发工具（此脚本不进入客户便携包）。

依赖：pip install cryptography

示例：
  py -3.14 scripts/license_issuer.py issue ^
    --key "G:\secure\private_key.pem" ^
    --device 447DAA40-52F5-DB2C-D3F8-3B1804726B7E ^
    --customer "ABC Automation" --days 365
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .license_records import LicenseRecordStore
except ImportError:
    from license_records import LicenseRecordStore


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def create_license_code(
    key_path: str | Path,
    device: str,
    customer: str = "",
    days: int = 365,
    perpetual: bool = False,
    license_id: str = "",
) -> tuple[str, dict]:
    """签发一个授权码，供命令行和桌面 UI 共用。"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise RuntimeError("缺少 cryptography：请先安装该依赖")

    key_path = Path(key_path)
    if not key_path.is_file():
        raise ValueError(f"找不到私钥文件：{key_path}")
    try:
        device = str(uuid.UUID((device or "").strip().strip("{}"))).upper()
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "System ID 格式不正确，应为 XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
        ) from exc
    if uuid.UUID(device).int == 0:
        raise ValueError("System ID 不能为全零")
    if not perpetual and days <= 0:
        raise ValueError("授权天数必须大于 0")
    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )
    now = datetime.now(timezone.utc)
    expires = None if perpetual else now + timedelta(days=days)
    payload = {
        "product": "twincat-agent",
        "schema": 2,
        "license_id": license_id.strip() or ("LIC-" + secrets.token_hex(5).upper()),
        "device_id": device,
        "customer": customer.strip(),
        "issued_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expires_at": (
            expires.isoformat(timespec="seconds").replace("+00:00", "Z")
            if expires else ""
        ),
        "features": ["agent"],
    }
    raw = _canonical(payload)
    signature = private_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    code = "TCAG1." + _b64u(raw) + "." + _b64u(signature)
    return code, payload


def issue(args: argparse.Namespace) -> int:
    key_value = args.key or os.environ.get("TC_AGENT_LICENSE_PRIVATE_KEY", "")
    if not key_value:
        raise SystemExit("请用 --key 指定私钥，或设置 TC_AGENT_LICENSE_PRIVATE_KEY")
    try:
        code, payload = create_license_code(
            key_path=key_value,
            device=args.device,
            customer=args.customer,
            days=args.days,
            perpetual=args.perpetual,
            license_id=args.license_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    try:
        LicenseRecordStore().save(code, payload)
    except Exception as exc:
        raise SystemExit(f"授权码已生成，但保存授权记录失败：{exc}") from exc
    print("\n设备码：", payload["device_id"])
    print("客户：", payload["customer"] or "（未填写）")
    print("授权编号：", payload["license_id"])
    print("有效期：", payload["expires_at"][:10] if payload["expires_at"] else "永久")
    print("\n授权码：\n")
    print(code)
    if args.out:
        Path(args.out).write_text(code + "\n", encoding="utf-8")
        print("\n已保存：", Path(args.out).resolve())
    return 0


def list_records(args: argparse.Namespace) -> int:
    try:
        records = LicenseRecordStore().search(args.query, args.limit)
    except Exception as exc:
        raise SystemExit(f"读取授权记录失败：{exc}") from exc
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    print("签发时间\t客户\tSystem ID\t授权编号\t有效期")
    for record in records:
        expires = record["expires_at"][:10] if record["expires_at"] else "永久"
        print(
            f"{record['issued_at'][:19]}\t{record['customer']}\t"
            f"{record['device_id']}\t{record['license_id']}\t{expires}"
        )
    print(f"\n共 {len(records)} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TwinCAT Agent 授权码签发工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("issue", help="为一台设备签发授权码")
    p.add_argument("--key", help="RSA 私钥 PEM；也可用环境变量 TC_AGENT_LICENSE_PRIVATE_KEY")
    p.add_argument("--device", required=True, help="客户授权页显示的 TwinCAT System ID")
    p.add_argument("--customer", default="", help="客户/公司名称")
    p.add_argument("--days", type=int, default=365, help="有效天数，默认 365")
    p.add_argument("--perpetual", action="store_true", help="永久授权")
    p.add_argument("--license-id", default="", help="自定义授权编号")
    p.add_argument("--out", help="把授权码同时保存到文本文件")
    p.set_defaults(func=issue)
    q = sub.add_parser("records", help="查询本机保存的授权记录")
    q.add_argument("--query", default="", help="按客户、System ID 或授权编号搜索")
    q.add_argument("--limit", type=int, default=500, help="最多返回条数，默认 500")
    q.add_argument("--json", action="store_true", help="输出完整 JSON（含授权码）")
    q.set_defaults(func=list_records)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
