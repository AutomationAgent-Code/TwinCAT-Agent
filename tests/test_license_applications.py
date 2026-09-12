import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from scripts.license_applications import (
    ApplicationImportError,
    BAIDU_INSTALLER_CODE,
    BAIDU_INSTALLER_URL,
    INSTALLER_RELEASE_URL,
    build_reply_message,
    build_reply_mailto,
    load_applications,
    normalize_application,
    send_reply_email,
)
from scripts.license_records import LicenseRecordStore


SYSTEM_ID = "4BED5A99-4F47-A872-A264-00489CF63F2C"


class LicenseApplicationTests(unittest.TestCase):
    def test_imports_website_export_csv_and_skips_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submissions.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "record_type", "created_at", "name", "email", "company",
                        "systemId", "version", "controller", "scenario",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "record_type": "feedback",
                    "name": "反馈用户",
                    "email": "feedback@example.com",
                    "systemId": "",
                })
                writer.writerow({
                    "record_type": "trial",
                    "created_at": "2026-09-02T10:00:00+00:00",
                    "name": "张工",
                    "email": "zhang@example.com",
                    "company": "示例自动化",
                    "systemId": SYSTEM_ID.lower(),
                    "version": "TwinCAT 3.1.4026",
                    "controller": "CX9240",
                    "scenario": "验证 PLC 编程",
                })
            applications = load_applications(path)
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0]["system_id"], SYSTEM_ID)
        self.assertEqual(applications[0]["email"], "zhang@example.com")

    def test_imports_single_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.json"
            path.write_text(json.dumps({
                "name": "李工",
                "email": "li@example.com",
                "company": "测试公司",
                "systemId": SYSTEM_ID,
            }), encoding="utf-8")
            applications = load_applications(path)
        self.assertEqual(applications[0]["name"], "李工")

    def test_rejects_invalid_system_id(self):
        with self.assertRaises(ApplicationImportError):
            normalize_application({"name": "用户", "systemId": "not-a-system-id"})

    def test_builds_mailto_reply_with_encoded_code(self):
        url = build_reply_mailto(
            {"name": "张工", "email": "zhang@example.com", "system_id": SYSTEM_ID},
            {
                "customer": "示例自动化",
                "license_id": "LIC-123",
                "device_id": SYSTEM_ID,
                "expires_at": "2026-10-02T12:00:00Z",
            },
            "TCAG1.payload.signature",
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "mailto")
        self.assertEqual(parsed.path, "zhang@example.com")
        self.assertIn("LIC-123", unquote(query["body"][0]))
        self.assertIn("TCAG1.payload.signature", unquote(query["body"][0]))
        self.assertIn(INSTALLER_RELEASE_URL, unquote(query["body"][0]))
        self.assertIn(BAIDU_INSTALLER_URL, unquote(query["body"][0]))
        self.assertIn(f"提取码：{BAIDU_INSTALLER_CODE}", unquote(query["body"][0]))
        self.assertIn("【客户安装包】", unquote(query["body"][0]))
        self.assertIn("【使用教程】", unquote(query["body"][0]))
        self.assertIn("TwinCAT XAE 初始界面", unquote(query["body"][0]))
        self.assertIn("顶端视图（View）打开TwinCAT Agent 授权页面", unquote(query["body"][0]))
        self.assertIn("【免责声明】", unquote(query["body"][0]))

    def test_record_store_migrates_and_persists_application(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LicenseRecordStore(Path(directory) / "records.db")
            store.save(
                "TCAG1.code.signature",
                {
                    "license_id": "LIC-123",
                    "customer": "示例自动化",
                    "device_id": SYSTEM_ID,
                    "issued_at": "2026-09-02T00:00:00Z",
                    "expires_at": "",
                },
                {"name": "张工", "email": "zhang@example.com"},
            )
            record = store.search()[0]
            latest = store.latest_for_device(SYSTEM_ID.lower())
        self.assertEqual(json.loads(record["application_json"])["email"], "zhang@example.com")
        self.assertEqual(latest["license_id"], "LIC-123")

    def test_builds_message_with_gmail_sender(self):
        message = build_reply_message(
            {"name": "张工", "email": "zhang@example.com", "system_id": SYSTEM_ID},
            {
                "customer": "示例自动化",
                "license_id": "LIC-123",
                "device_id": SYSTEM_ID,
                "expires_at": "",
            },
            "TCAG1.payload.signature",
            "zxctian@gmail.com",
        )
        self.assertEqual(message["From"], "zxctian@gmail.com")
        self.assertEqual(message["To"], "zhang@example.com")
        self.assertIn("TCAG1.payload.signature", message.get_content())
        self.assertIn("【使用教程】", message.get_content())
        self.assertIn("【免责声明】", message.get_content())

    def test_sends_through_gmail_smtp_without_persisting_password(self):
        class FakeSmtp:
            def __init__(self, host, port, timeout):
                self.args = (host, port, timeout)
                self.logged_in = None
                self.message = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def login(self, sender, password):
                self.logged_in = (sender, password)

            def send_message(self, message):
                self.message = message

        instances = []

        def make_smtp(*args, **kwargs):
            smtp = FakeSmtp(*args, **kwargs)
            instances.append(smtp)
            return smtp

        with patch("scripts.license_applications.smtplib.SMTP_SSL", side_effect=make_smtp):
            send_reply_email(
                {"name": "张工", "email": "zhang@example.com", "system_id": SYSTEM_ID},
                {
                    "customer": "示例自动化",
                    "license_id": "LIC-123",
                    "device_id": SYSTEM_ID,
                    "expires_at": "",
                },
                "TCAG1.payload.signature",
                "zxctian@gmail.com",
                "abcd efgh",
            )
        smtp = instances[0]
        self.assertEqual(smtp.args, ("smtp.gmail.com", 465, 20))
        self.assertEqual(smtp.logged_in, ("zxctian@gmail.com", "abcdefgh"))
        self.assertEqual(smtp.message["To"], "zhang@example.com")


if __name__ == "__main__":
    unittest.main()
