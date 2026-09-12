"""Read-only validation of an explicitly selected saved HMI view via CDP."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from websockets.sync.client import connect


def _browser() -> str:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError("Microsoft Edge or Google Chrome was not found")


def _debug_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _json_url(url: str, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


class _Cdp:
    def __init__(self, websocket_url: str):
        self.socket = connect(websocket_url, open_timeout=10, close_timeout=3)
        self.sequence = 0
        self.events = []

    def call(self, method: str, params: dict | None = None, timeout: float = 20):
        self.sequence += 1
        call_id = self.sequence
        self.socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv(timeout=max(.1, deadline - time.monotonic())))
            if message.get("id") == call_id:
                if message.get("error"):
                    raise RuntimeError(f"Browser DevTools {method} failed: {message['error'].get('message')}")
                return message.get("result") or {}
            if message.get("method"):
                self.events.append(message)
        raise TimeoutError(f"Browser DevTools {method} timed out")

    def close(self):
        self.socket.close()


def _diagnostics(events: list[dict], width: int) -> list[dict]:
    output = []
    for event in events:
        method, params = event.get("method"), event.get("params") or {}
        item = None
        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            exception = detail.get("exception") or {}
            item = {"severity": "error", "kind": "javascript-exception",
                    "message": str(exception.get("description") or detail.get("text") or ""),
                    "url": str(detail.get("url") or ""), "line": int(detail.get("lineNumber") or 0) + 1}
        elif method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "warning"}:
            parts = [str(arg.get("value", arg.get("description", arg.get("type", ""))))
                     for arg in params.get("args") or []]
            item = {"severity": params["type"], "kind": "console", "message": " ".join(parts)}
        elif method == "Log.entryAdded" and (params.get("entry") or {}).get("level") in {"error", "warning"}:
            entry = params["entry"]
            item = {"severity": entry["level"], "kind": "browser-log",
                    "message": str(entry.get("text") or ""), "url": str(entry.get("url") or "")}
        elif method == "Network.loadingFailed" and not params.get("canceled"):
            error = str(params.get("errorText") or "")
            if "ERR_ABORTED" not in error.upper():
                item = {"severity": "error", "kind": "resource-load-failed", "message": error}
        elif method == "Network.responseReceived" and int((params.get("response") or {}).get("status") or 0) >= 400:
            response = params["response"]
            item = {"severity": "error", "kind": "http-error",
                    "message": f"HTTP {int(response['status'])}", "url": str(response.get("url") or "")}
        elif method == "Network.webSocketFrameError":
            item = {"severity": "error", "kind": "websocket-frame-error",
                    "message": str(params.get("errorMessage") or "")}
        if item:
            item.setdefault("url", "")
            item.setdefault("line", 0)
            item["viewport_width"] = width
            output.append(item)
    return output


_METRICS = r"""(() => {
 const controls=Array.from(document.querySelectorAll('[data-tchmi-type]'));
 const ids=controls.map(x=>x.id).filter(Boolean);
 const visible=controls.filter(x=>{const s=getComputedStyle(x),r=x.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;});
 let symbols=0; for(const element of controls) for(const attr of element.attributes) if(/%(?:s|i|ctrl)%/.test(attr.value)) symbols++;
 return {ready_state:document.readyState,title:document.title,url:location.href,
  hmi_framework_loaded:typeof window.TcHmi==='object',hmi_server_api_loaded:!!(window.TcHmi&&TcHmi.Server),
  main_container_count:document.querySelectorAll('.tchmi-main-hmi-container').length,
  view_count:controls.filter(x=>/TcHmiView$/.test(x.getAttribute('data-tchmi-type')||'')).length,
  control_count:controls.length,visible_control_count:visible.length,control_ids:ids,
  duplicate_ids:Array.from(new Set(ids.filter((x,i)=>ids.indexOf(x)!==i))),
  control_types:Array.from(new Set(controls.map(x=>x.getAttribute('data-tchmi-type')).filter(Boolean))),
  symbol_expression_count:symbols,viewport_width:innerWidth,viewport_height:innerHeight,
  document_width:document.documentElement.scrollWidth,document_height:document.documentElement.scrollHeight,
  horizontal_overflow:document.documentElement.scrollWidth>innerWidth+1,
  vertical_overflow:document.documentElement.scrollHeight>innerHeight+1}; })()"""


def _registered_view(project_file: str, entry_page: str) -> tuple[Path, str]:
    project = Path(project_file).resolve()
    root = project.parent
    relative = entry_page.replace("\\", "/").strip()
    if not relative.lower().endswith(".view") or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("entry_page must be a saved project-relative .view path")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"Saved HMI entry page was not found: {relative}")
    includes = {str(node.attrib.get("Include") or "").replace("\\", "/").casefold()
                for node in ET.parse(project).getroot().iter()
                if node.tag.rsplit("}", 1)[-1] == "Content"}
    if relative.casefold() not in includes:
        raise ValueError(f"HMI entry page is not registered in the project: {relative}")
    return target, relative


def validate_entry_page(project: str, entry_page: str, widths: list[int], height: int,
                        settle_ms: int) -> dict:
    """Load one explicit view without clicks, writes, publishing or server control."""
    from . import _ps_bridge as ps
    info = ps.com_hmi_project_info(project)
    _, relative = _registered_view(info["project_file"], entry_page)
    runtime = ps.com_hmi_runtime_info(info["project_file"])
    if runtime.get("application_ready") is not True:
        raise RuntimeError("HMI Engineering Server/application is not already ready")
    values = list(dict.fromkeys(int(value) for value in (widths or [1280])))
    if not values or len(values) > 4 or any(value < 320 or value > 3840 for value in values):
        raise ValueError("widths must contain one to four values between 320 and 3840")
    if not 240 <= int(height) <= 2160 or not 500 <= int(settle_ms) <= 30000:
        raise ValueError("height/settle_ms is outside the supported range")
    executable, port = _browser(), _debug_port()
    profile = Path(tempfile.mkdtemp(prefix="TwinCATAgent-HmiBrowser-"))
    process = subprocess.Popen([
        executable, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-background-networking",
        "--remote-allow-origins=*", f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}", "about:blank",
    ], creationflags=0x08000000 if os.name == "nt" else 0,
       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        version = None
        for _ in range(60):
            try:
                version = _json_url(f"http://127.0.0.1:{port}/json/version")
                break
            except Exception:
                time.sleep(.1)
        if not version:
            raise RuntimeError("Headless browser DevTools endpoint did not become ready")
        targets = _json_url(f"http://127.0.0.1:{port}/json/list")
        target = next((item for item in targets if item.get("type") == "page"), None)
        if not target:
            target = _json_url(f"http://127.0.0.1:{port}/json/new?about%3Ablank", "PUT")
        cdp = _Cdp(target["webSocketDebuggerUrl"])
        for domain in ("Runtime", "Page", "Network", "Log"):
            cdp.call(domain + ".enable")
        viewport_results, all_diagnostics = [], []
        literal = json.dumps(relative)
        load_expression = (
            "new Promise(resolve=>{const done=d=>resolve({ok:!(d&&d.error),error:d&&d.error?String(d.error):''});"
            "if(!(window.TcHmi&&TcHmi.View&&TcHmi.View.load)){resolve({ok:false,error:'TcHmi.View.load unavailable'});return;}"
            f"TcHmi.View.load({literal},done);}})"
        )
        for width in values:
            cdp.events = []
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": int(height),
                     "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Page.navigate", {"url": runtime["application_url"]})
            time.sleep(settle_ms / 1000)
            loaded = cdp.call("Runtime.evaluate", {"expression": load_expression,
                "returnByValue": True, "awaitPromise": True}, timeout=20)
            load_result = ((loaded.get("result") or {}).get("value") or {})
            time.sleep(min(settle_ms, 3000) / 1000)
            evaluated = cdp.call("Runtime.evaluate", {"expression": _METRICS,
                "returnByValue": True, "awaitPromise": True})
            metrics = ((evaluated.get("result") or {}).get("value") or {})
            diagnostics = _diagnostics(cdp.events, width)
            all_diagnostics.extend(diagnostics)
            hard = [item for item in diagnostics if item["severity"] == "error"]
            page_loaded = (load_result.get("ok") is True and metrics.get("ready_state") == "complete"
                           and metrics.get("hmi_framework_loaded") is True
                           and int(metrics.get("main_container_count") or 0) > 0)
            viewport_results.append({"requested_width": width, "requested_height": int(height),
                "loaded": page_loaded, "target_view_loaded": load_result.get("ok") is True,
                "target_view_load_error": load_result.get("error") or "", "metrics": metrics,
                "diagnostics": diagnostics, "error_count": len(hard),
                "valid": page_loaded and not hard and not metrics.get("duplicate_ids")})
        success = all(item["valid"] for item in viewport_results)
        result = {"status": "passed" if success else "failed", "success": success,
            "project": info.get("name"), "project_file": info.get("project_file"),
            "application_url": runtime["application_url"], "browser_executable": executable,
            "browser_version": str(version.get("Browser") or ""), "viewport_results": viewport_results,
            "diagnostics": all_diagnostics,
            "error_count": sum(item["severity"] == "error" for item in all_diagnostics),
            "warning_count": sum(item["severity"] == "warning" for item in all_diagnostics),
            "requested_entry_page": relative,
            "loaded_view": relative if all(item["target_view_loaded"] for item in viewport_results) else "",
            "readonly": True, "interaction_performed": False, "plc_write_performed": False,
            "publish_performed": False, "server_control_performed": False}
        from .hmi_browser_evidence import compare_controls
        return compare_controls(result, info["project_file"])
    finally:
        if cdp:
            try:
                cdp.call("Browser.close", timeout=3)
            except Exception:
                pass
            try:
                cdp.close()
            except Exception:
                pass
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(profile, ignore_errors=True)
