"""TwinCAT Agent 供应商授权工具桌面 UI。"""

from __future__ import annotations

import json
import os
import re
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from .license_issuer import create_license_code
    from .license_records import LicenseRecordStore
    from .license_applications import (
        ApplicationImportError,
        build_reply_mailto,
        load_applications,
        send_reply_email,
    )
except ImportError:
    from license_issuer import create_license_code
    from license_records import LicenseRecordStore
    from license_applications import (
        ApplicationImportError,
        build_reply_mailto,
        load_applications,
        send_reply_email,
    )

APP_TITLE = "TwinCAT Agent 授权工具"
DEFAULT_SENDER = "zxctian@gmail.com"
BG = "#f3f5f7"
PANEL = "#ffffff"
TEXT = "#20242a"
MUTED = "#68707c"
ACCENT = "#c63f32"
ACCENT_ACTIVE = "#a93228"
BORDER = "#d9dee5"


def _settings_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "TwinCAT Agent License Tool" / "settings.json"


class LicenseIssuerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("780x740")
        self.root.minsize(680, 620)
        self.root.configure(bg=BG)

        self.key_var = tk.StringVar()
        self.device_var = tk.StringVar()
        self.customer_var = tk.StringVar()
        self.applicant_var = tk.StringVar()
        self.email_var = tk.StringVar()
        self.sender_var = tk.StringVar(value=DEFAULT_SENDER)
        self.app_password_var = tk.StringVar()
        self.days_var = tk.StringVar(value="365")
        self.perpetual_var = tk.BooleanVar(value=False)
        self.license_id_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="填写信息后点击“生成授权码”")
        self.last_payload: dict = {}
        self.application_data: dict = {}
        self.existing_license: dict | None = None
        self.application_summary_var = tk.StringVar(value="未导入申请表")
        self.records = LicenseRecordStore()
        self.records_window: LicenseRecordsWindow | None = None
        self._load_settings()
        self._build()

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=PANEL, foreground=TEXT, font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), foreground="white", background=ACCENT)
        style.map("Accent.TButton", background=[("active", ACCENT_ACTIVE), ("pressed", ACCENT_ACTIVE)])

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        panel = ttk.Frame(outer, style="Panel.TFrame", padding=20)
        panel.pack(fill="both", expand=True)

        brand = ttk.Frame(panel, style="Panel.TFrame")
        brand.pack(fill="x", pady=(0, 14))
        logo = tk.Label(brand, text="TC", fg="white", bg=ACCENT, width=3, height=1,
                        font=("Segoe UI", 15, "bold"), padx=4, pady=7)
        logo.pack(side="left", padx=(0, 12))
        titles = ttk.Frame(brand, style="Panel.TFrame")
        titles.pack(side="left", fill="x", expand=True)
        ttk.Label(titles, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="仅供供应商签发设备绑定授权码 · 私钥不会写入授权码",
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Button(brand, text="查询授权记录", command=self.open_records).pack(
            side="right", padx=(12, 0)
        )
        ttk.Button(brand, text="导入申请表…", command=self.import_application).pack(
            side="right", padx=(12, 0)
        )

        self._label(panel, "RSA 私钥")
        key_row = ttk.Frame(panel, style="Panel.TFrame")
        key_row.pack(fill="x", pady=(0, 8))
        ttk.Button(key_row, text="选择…", command=self.choose_key).pack(side="right", padx=(8, 0))
        ttk.Entry(key_row, textvariable=self.key_var).pack(side="left", fill="x", expand=True)

        self._label(panel, "客户 TwinCAT System ID")
        device_entry = ttk.Entry(panel, textvariable=self.device_var, font=("Cascadia Mono", 10))
        device_entry.pack(fill="x", pady=(0, 8))
        ttk.Label(panel, textvariable=self.application_summary_var,
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(0, 8))

        two = ttk.Frame(panel, style="Panel.TFrame")
        two.pack(fill="x")
        left = ttk.Frame(two, style="Panel.TFrame")
        right = ttk.Frame(two, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True, padx=(0, 7))
        right.pack(side="left", fill="x", expand=True, padx=(7, 0))
        self._label(left, "客户 / 公司名称")
        ttk.Entry(left, textvariable=self.customer_var).pack(fill="x", pady=(0, 8))
        self._label(right, "授权编号（可留空自动生成）")
        ttk.Entry(right, textvariable=self.license_id_var).pack(fill="x", pady=(0, 8))

        contact = ttk.Frame(panel, style="Panel.TFrame")
        contact.pack(fill="x")
        left = ttk.Frame(contact, style="Panel.TFrame")
        right = ttk.Frame(contact, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True, padx=(0, 7))
        right.pack(side="left", fill="x", expand=True, padx=(7, 0))
        self._label(left, "申请人")
        ttk.Entry(left, textvariable=self.applicant_var).pack(fill="x", pady=(0, 8))
        self._label(right, "回复邮箱")
        ttk.Entry(right, textvariable=self.email_var).pack(fill="x", pady=(0, 8))

        mail = ttk.Frame(panel, style="Panel.TFrame")
        mail.pack(fill="x")
        left = ttk.Frame(mail, style="Panel.TFrame")
        right = ttk.Frame(mail, style="Panel.TFrame")
        left.pack(side="left", fill="x", expand=True, padx=(0, 7))
        right.pack(side="left", fill="x", expand=True, padx=(7, 0))
        self._label(left, "Gmail 发件邮箱")
        ttk.Entry(left, textvariable=self.sender_var).pack(fill="x", pady=(0, 3))
        ttk.Label(left, text="默认：zxctian@gmail.com", style="Muted.TLabel").pack(anchor="w")
        self._label(right, "应用专用密码（本次运行）")
        ttk.Entry(right, textvariable=self.app_password_var, show="*").pack(fill="x", pady=(0, 3))
        ttk.Label(right, text="不会保存到配置文件", style="Muted.TLabel").pack(anchor="w")

        term = ttk.Frame(panel, style="Panel.TFrame")
        term.pack(fill="x", pady=(0, 10))
        self._label(term, "授权期限")
        row = ttk.Frame(term, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="有效天数").pack(side="left")
        self.days_entry = ttk.Entry(row, textvariable=self.days_var, width=12)
        self.days_entry.pack(side="left", padx=(8, 18))
        ttk.Checkbutton(row, text="永久授权", variable=self.perpetual_var,
                        command=self.toggle_perpetual).pack(side="left")

        ttk.Button(panel, text="生成授权码", style="Accent.TButton",
                   command=self.generate).pack(fill="x", ipady=6, pady=(2, 9))

        ttk.Label(panel, textvariable=self.summary_var, style="Muted.TLabel",
                  wraplength=660).pack(anchor="w", pady=(0, 7))

        # Reserve the bottom action bar before the expandable output area so it
        # remains visible on 1366×768 laptops and at high DPI.
        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(actions, text="发送回复邮件", command=self.send_email).pack(side="left")
        ttk.Button(actions, text="生成回复邮件", command=self.reply_email).pack(side="left")
        ttk.Button(actions, text="复制授权码", command=self.copy_code).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="保存为文件…", command=self.save_code).pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="私钥只在生成瞬间读取，请勿发送给客户",
                  style="Muted.TLabel").pack(side="right")

        self.output = tk.Text(panel, height=6, wrap="word", font=("Cascadia Mono", 9),
                              bg="#f7f8fa", fg=TEXT, insertbackground=TEXT,
                              relief="solid", borderwidth=1, highlightthickness=0)
        self.output.pack(fill="both", expand=True)
        self.toggle_perpetual()

    @staticmethod
    def _label(parent, text: str) -> None:
        ttk.Label(parent, text=text).pack(anchor="w", pady=(0, 5))

    def _load_settings(self) -> None:
        try:
            data = json.loads(_settings_path().read_text(encoding="utf-8"))
            self.key_var.set(data.get("last_key_path", ""))
        except Exception:
            env_key = os.environ.get("TC_AGENT_LICENSE_PRIVATE_KEY", "")
            if env_key:
                self.key_var.set(env_key)

    def _save_settings(self) -> None:
        try:
            path = _settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"last_key_path": self.key_var.get().strip()},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def choose_key(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 TwinCAT Agent RSA 私钥",
            filetypes=[("PEM 私钥", "*.pem"), ("所有文件", "*.*")],
        )
        if selected:
            self.key_var.set(selected)
            self._save_settings()

    def toggle_perpetual(self) -> None:
        self.days_entry.configure(state="disabled" if self.perpetual_var.get() else "normal")

    def import_application(self) -> None:
        path = filedialog.askopenfilename(
            title="导入 TwinCAT Agent 申请表",
            filetypes=[("申请表 CSV", "*.csv"), ("申请表 JSON", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            applications = [
                self._mark_existing_license(application)
                for application in load_applications(path)
            ]
            if len(applications) == 1:
                self.apply_application(applications[0])
            else:
                ApplicationPickerWindow(self, applications)
        except ApplicationImportError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)

    def _mark_existing_license(self, application: dict) -> dict:
        marked = dict(application)
        marked["_existing_license"] = self.records.latest_for_device(
            application.get("system_id", "")
        )
        return marked

    @staticmethod
    def _authorization_label(record: dict | None) -> str:
        if not record:
            return "未授权"
        expires = record.get("expires_at") or "永久"
        if expires == "永久":
            return f"已永久授权（{record.get('license_id', '')}）"
        return f"已授权至 {str(expires)[:10]}（{record.get('license_id', '')}）"

    def apply_application(self, application: dict) -> None:
        self.application_data = {
            key: value for key, value in application.items()
            if not key.startswith("_")
        }
        self.existing_license = application.get("_existing_license")
        self.device_var.set(application.get("system_id", ""))
        self.customer_var.set(application.get("company", ""))
        self.applicant_var.set(application.get("name", ""))
        self.email_var.set(application.get("email", ""))
        details = [
            value for value in (
                application.get("version"),
                application.get("controller"),
                application.get("scenario"),
            ) if value
        ]
        self.application_summary_var.set(
            "已导入申请：" + (application.get("name") or "未填写姓名")
            + (" · " + " / ".join(details) if details else "")
            + " · " + self._authorization_label(self.existing_license)
        )

    def generate(self) -> None:
        try:
            customer = self.customer_var.get().strip()
            if not customer:
                raise ValueError("请填写客户或公司名称")
            existing = self.records.latest_for_device(self.device_var.get())
            if existing and not messagebox.askyesno(
                APP_TITLE,
                f"该 System ID 已有历史授权：\n"
                f"{self._authorization_label(existing)}\n\n"
                "仍要重新签发一条新授权吗？",
                parent=self.root,
            ):
                return
            days = 1 if self.perpetual_var.get() else int(self.days_var.get().strip())
            code, payload = create_license_code(
                key_path=self.key_var.get().strip(),
                device=self.device_var.get(),
                customer=customer,
                days=days,
                perpetual=self.perpetual_var.get(),
                license_id=self.license_id_var.get(),
            )
            self.records.save(code, payload, self.application_data)
            self.device_var.set(payload["device_id"])
            self.license_id_var.set(payload["license_id"])
            self.last_payload = payload
            self.output.delete("1.0", "end")
            self.output.insert("1.0", code)
            expiry = payload["expires_at"][:10] if payload["expires_at"] else "永久"
            self.summary_var.set(
                f"✓ 已生成 · {payload['customer']} · {payload['license_id']} · 有效期至 {expiry}"
            )
            self._save_settings()
        except Exception as exc:
            self.summary_var.set("生成失败")
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)

    def open_records(self) -> None:
        if self.records_window and self.records_window.window.winfo_exists():
            self.records_window.window.deiconify()
            self.records_window.window.lift()
            self.records_window.search_entry.focus_set()
            return
        self.records_window = LicenseRecordsWindow(self)

    def load_record(self, record: dict) -> None:
        try:
            payload = json.loads(record["payload_json"])
        except Exception:
            payload = {
                "license_id": record["license_id"],
                "customer": record["customer"],
                "device_id": record["device_id"],
                "issued_at": record["issued_at"],
                "expires_at": record["expires_at"],
            }
        self.device_var.set(record["device_id"])
        self.customer_var.set(record["customer"])
        try:
            application = json.loads(record.get("application_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            application = {}
        if application:
            self.apply_application(application)
        else:
            self.application_data = {}
            self.existing_license = record
            self.applicant_var.set("")
            self.email_var.set("")
            self.application_summary_var.set("未关联申请表")
        if application:
            self.existing_license = record
            self.application_summary_var.set(
                "已关联申请：" + (application.get("name") or "未填写姓名")
                + " · " + self._authorization_label(record)
            )
        self.license_id_var.set(record["license_id"])
        self.last_payload = payload
        self.output.delete("1.0", "end")
        self.output.insert("1.0", record["code"])
        self.summary_var.set(f"✓ 已载入授权记录 · {record['license_id']}")

    def _code(self) -> str:
        code = self.output.get("1.0", "end").strip()
        if not code.startswith("TCAG1."):
            raise ValueError("请先生成授权码")
        return code

    def copy_code(self) -> None:
        try:
            code = self._code()
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update()
            self.summary_var.set("✓ 授权码已复制到剪贴板")
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)

    def reply_email(self) -> None:
        try:
            code = self._code()
            application = dict(self.application_data)
            application["email"] = self.email_var.get().strip()
            application["name"] = self.applicant_var.get().strip()
            application["system_id"] = self.device_var.get().strip()
            if not self.last_payload:
                raise ValueError("请先生成或载入一条授权码")
            webbrowser.open(build_reply_mailto(application, self.last_payload, code))
            self.summary_var.set("✓ 已打开回复邮件草稿，请检查内容后发送")
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)

    def send_email(self) -> None:
        try:
            code = self._code()
            if not self.last_payload:
                raise ValueError("请先生成或载入一条授权码")
            application = dict(self.application_data)
            application["email"] = self.email_var.get().strip()
            application["name"] = self.applicant_var.get().strip()
            application["system_id"] = self.device_var.get().strip()
            confirmed = messagebox.askyesno(
                APP_TITLE,
                f"确定通过 {self.sender_var.get().strip()} 发送授权邮件给\n"
                f"{application.get('email') or '（未填写收件人）'}？\n\n"
                "发送后邮件将实际离开发件账户，请确认授权码和有效期无误。",
                parent=self.root,
            )
            if not confirmed:
                return
            send_reply_email(
                application,
                self.last_payload,
                code,
                self.sender_var.get().strip(),
                self.app_password_var.get(),
            )
            self.app_password_var.set("")
            self.summary_var.set("✓ 授权回复邮件已发送")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)

    def save_code(self) -> None:
        try:
            code = self._code()
            customer = re.sub(
                r'[\\/:*?"<>|]+', "_", self.customer_var.get().strip()
            ) or "customer"
            license_id = self.last_payload.get("license_id", "license")
            path = filedialog.asksaveasfilename(
                title="保存授权码",
                defaultextension=".txt",
                initialfile=f"{customer}-{license_id}.txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
            if path:
                Path(path).write_text(code + "\n", encoding="utf-8")
                self.summary_var.set(f"✓ 已保存到 {path}")
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)


class ApplicationPickerWindow:
    def __init__(self, app: LicenseIssuerApp, applications: list[dict]) -> None:
        self.app = app
        self.applications = applications
        self.window = tk.Toplevel(app.root)
        self.window.title("选择申请")
        self.window.geometry("980x430")
        self.window.minsize(760, 330)
        self.window.transient(app.root)

        panel = ttk.Frame(self.window, padding=14)
        panel.pack(fill="both", expand=True)
        ttk.Label(
            panel,
            text=f"文件中找到 {len(applications)} 条临时使用申请，请选择一条",
        ).pack(anchor="w", pady=(0, 10))
        frame = ttk.Frame(panel)
        frame.pack(fill="both", expand=True)
        columns = ("created_at", "name", "email", "company", "system_id", "status")
        self.table = ttk.Treeview(frame, columns=columns, show="headings")
        headings = {
            "created_at": "提交时间",
            "name": "申请人",
            "email": "邮箱",
            "company": "公司 / 团队",
            "system_id": "TwinCAT System ID",
            "status": "授权状态",
        }
        widths = {
            "created_at": 145,
            "name": 100,
            "email": 210,
            "company": 150,
            "system_id": 285,
            "status": 170,
        }
        for name in columns:
            self.table.heading(name, text=headings[name])
            self.table.column(name, width=widths[name], minwidth=80)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for index, application in enumerate(applications):
            self.table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    application.get("created_at", "")[:19].replace("T", " "),
                    application.get("name", ""),
                    application.get("email", ""),
                    application.get("company", ""),
                    application.get("system_id", ""),
                    LicenseIssuerApp._authorization_label(
                        application.get("_existing_license")
                    ),
                ),
            )
        self.table.bind("<Double-1>", lambda _event: self.select())
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="导入选中申请", command=self.select).pack(side="left")
        ttk.Button(actions, text="取消", command=self.window.destroy).pack(side="right")

    def select(self) -> None:
        selected = self.table.selection()
        if not selected:
            messagebox.showwarning("选择申请", "请先选择一条申请", parent=self.window)
            return
        self.app.apply_application(self.applications[int(selected[0])])
        self.window.destroy()


class LicenseRecordsWindow:
    def __init__(self, app: LicenseIssuerApp) -> None:
        self.app = app
        self.rows: dict[str, dict] = {}
        self.window = tk.Toplevel(app.root)
        self.window.title("TwinCAT Agent · 授权记录")
        self.window.geometry("1050x560")
        self.window.minsize(820, 420)
        self.window.configure(bg=BG)
        self.window.transient(app.root)
        self.query_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在读取…")
        self._build()
        self.refresh()

    def _build(self) -> None:
        panel = ttk.Frame(self.window, style="Panel.TFrame", padding=16)
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            header, text="授权记录", style="Title.TLabel"
        ).pack(side="left")
        ttk.Label(
            header,
            textvariable=self.status_var,
            style="Muted.TLabel",
        ).pack(side="right")

        search = ttk.Frame(panel, style="Panel.TFrame")
        search.pack(fill="x", pady=(0, 10))
        self.search_entry = ttk.Entry(search, textvariable=self.query_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _event: self.refresh())
        ttk.Button(search, text="查询", command=self.refresh).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            search,
            text="清空条件",
            command=self.clear_query,
        ).pack(side="left", padx=(8, 0))

        table_frame = ttk.Frame(panel, style="Panel.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("issued_at", "customer", "device_id", "license_id", "expires_at")
        self.table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "issued_at": "签发时间",
            "customer": "客户 / 公司",
            "device_id": "TwinCAT System ID",
            "license_id": "授权编号",
            "expires_at": "有效期",
        }
        widths = {
            "issued_at": 145,
            "customer": 165,
            "device_id": 285,
            "license_id": 145,
            "expires_at": 100,
        }
        for name in columns:
            self.table.heading(name, text=headings[name])
            self.table.column(name, width=widths[name], minwidth=80)
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.table.yview
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.bind("<Double-1>", lambda _event: self.load_selected())

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(
            actions, text="载入到发码界面", command=self.load_selected
        ).pack(side="left")
        ttk.Button(
            actions, text="复制授权码", command=self.copy_selected
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions, text="关闭", command=self.window.destroy
        ).pack(side="right")

    def clear_query(self) -> None:
        self.query_var.set("")
        self.refresh()

    def refresh(self) -> None:
        try:
            records = self.app.records.search(self.query_var.get())
            self.table.delete(*self.table.get_children())
            self.rows.clear()
            for index, record in enumerate(records):
                item_id = f"record-{index}"
                self.rows[item_id] = record
                expires = (
                    record["expires_at"][:10]
                    if record["expires_at"]
                    else "永久"
                )
                self.table.insert(
                    "",
                    "end",
                    iid=item_id,
                    values=(
                        record["issued_at"][:19].replace("T", " "),
                        record["customer"],
                        record["device_id"],
                        record["license_id"],
                        expires,
                    ),
                )
            self.status_var.set(f"共 {len(records)} 条记录")
        except Exception as exc:
            self.status_var.set("读取失败")
            messagebox.showerror(
                APP_TITLE, f"读取授权记录失败：{exc}", parent=self.window
            )

    def _selected(self) -> dict:
        selected = self.table.selection()
        if not selected:
            raise ValueError("请先选择一条授权记录")
        return self.rows[selected[0]]

    def load_selected(self) -> None:
        try:
            record = self._selected()
            self.app.load_record(record)
            self.window.destroy()
            self.app.root.lift()
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.window)

    def copy_selected(self) -> None:
        try:
            record = self._selected()
            self.window.clipboard_clear()
            self.window.clipboard_append(record["code"])
            self.window.update()
            self.status_var.set(f"✓ 已复制 {record['license_id']}")
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.window)


def main() -> int:
    root = tk.Tk()
    if "--smoke-test" in sys.argv:
        root.withdraw()
        app = LicenseIssuerApp(root)
        app.open_records()
        root.update_idletasks()
        root.destroy()
        return 0
    LicenseIssuerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
