"""
tc_agent.config — persisted provider / model configuration for TwinCAT Agent.

CC-Switch-style customer-key provider profiles. The self-built brain supports
OpenAI-compatible and Anthropic-compatible endpoints: DeepSeek, Anthropic,
Moonshot Kimi, 智谱 GLM, etc.

Stored in the per-user application-data directory, outside replaceable program
files.  Older builds kept config.json next to this module; that location is
read once for migration when the stable file does not exist.

Config shape:
  {
    "active_provider": "<id>",
    "providers": [
      {"id","name","kind":"login"|"api","base_url","api_key","model"}, ...
    ],
    "perm_mode": "plan|ask|accept|auto",
    "code_style": "project|standard|ham",
    "language": "zh|en"
  }
Legacy login profiles are removed during migration. API keys are NEVER sent to
the UI.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

LEGACY_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
) / "TwinCAT Agent" / "config.json"
CONFIG_PATH = DEFAULT_CONFIG_PATH

LOGIN_ID = "login"

# Built-in Claude model suggestions (used as datalist hints for the model field).
MODELS: list[dict] = [
    {"id": "", "label": "默认 (Claude Code 配置)"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
    {"id": "claude-fable-5", "label": "Fable 5"},
]

# Quick-add presets. Model names are suggestions only (the model field is free
# text, so any current model id works). Key is filled in by the user.
PROVIDER_TEMPLATES: list[dict] = [
    {"key": "openai", "name": "OpenAI 官方（Codex / Responses）",
     "base_url": "https://api.openai.com/v1", "protocol": "responses",
     "thinking": "auto", "vision": True, "models": ["gpt-5.3-codex"]},
    {"key": "anthropic", "name": "Anthropic 官方", "base_url": "",
     "models": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5"]},
    {"key": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
     "thinking": "off",
     "models": ["deepseek-v4-flash", "deepseek-v4-pro",
                "deepseek-chat", "deepseek-reasoner"]},
    {"key": "moonshot", "name": "Moonshot Kimi", "base_url": "https://api.moonshot.cn/anthropic",
     "models": ["kimi-k2-0905-preview", "kimi-k2-turbo-preview", "kimi-latest",
                "moonshot-v1-128k", "moonshot-v1-32k", "moonshot-v1-8k"]},
    {"key": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/anthropic",
     "models": ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4.5-flash", "glm-4-plus"]},
    {"key": "qwen", "name": "通义千问 Qwen (百炼)",
     "base_url": "https://dashscope.aliyuncs.com/api/v2/apps/claude-code-proxy",
     "models": ["qwen3-max", "qwen-max", "qwen-plus", "qwen3-coder-plus", "qwen-turbo"]},
    {"key": "minimax", "name": "MiniMax", "base_url": "https://api.minimaxi.com/anthropic",
     "models": ["MiniMax-M2", "MiniMax-Text-01"]},
    {"key": "siliconflow", "name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/",
     "models": ["deepseek-ai/DeepSeek-V3.2-Exp", "zai-org/GLM-4.6", "moonshotai/Kimi-K2-Instruct-0905",
                "Qwen/Qwen3-235B-A22B-Instruct-2507"]},
    {"key": "custom", "name": "自定义", "base_url": "", "models": []},
]

# Permission modes surfaced in the footer selector. `sdk` maps onto the
# claude-agent-sdk `permission_mode`; "ask" additionally routes mutating tools
# through the panel's approval callback (read-only tools auto-allowed).
PERM_MODES: list[dict] = [
    {"id": "plan",   "sdk": "plan",              "label": "📋 计划",    "hint": "只规划分析，不执行改动"},
    {"id": "ask",    "sdk": "default",           "label": "🙋 询问",    "hint": "改动前逐个确认，只读自动放行"},
    {"id": "accept", "sdk": "acceptEdits",       "label": "✏️ 编辑放行", "hint": "自动改代码，其他敏感操作确认"},
    {"id": "auto",   "sdk": "default",           "label": "⚡ 自动（受保护）", "hint": "低风险操作自动执行，高危操作仍需确认"},
]

MCP_TRANSPORTS = {
    "stdio": "本地程序（stdio）",
    "streamable_http": "远程服务（Streamable HTTP）",
}
MCP_SERVER_NAME_MAX = 80
MCP_VALUE_MAX = 4096

# PLC code-style profiles surfaced as a compact selector in the Agent panel.
# ``prompt`` is backend-only; public_settings exposes just id/label/hint.
CODE_STYLES: list[dict] = [
    {
        "id": "project", "label": "🎯 项目风格", "hint": "先学习当前 XAE 项目的命名和结构，再保持一致",
        "prompt": (
            "编程风格选择为【项目风格】。修改或新增 PLC 代码前，先读取当前项目中相邻、"
            "同类对象，归纳其命名、Method 分层、状态机和注释习惯；优先保持项目一致。"
            "若项目没有可参考实现，再退回标准规范。"
        ),
    },
    {
        "id": "standard", "label": "📐 标准风格", "hint": "使用 TwinCAT Agent 通用 PLC 编码规范",
        "prompt": (
            "编程风格选择为【标准风格】。遵循项目 docs/plc_coding_standard.md：英文标识符、"
            "中文注释、类型前缀、事务型 Done/Busy/Error/ErrorId、CASE 状态机和集中错误码。"
            "声明区按 Input/Output/InOut/Internal/Constant 顺序分区并标明单位范围；实现区先集中设置子 FB 输入，"
            "每个周期实例只调用一次，再统一读取输出。事务型 FB 必须包含超时和撤销触发后的复位路径。"
        ),
    },
    {
        "id": "ham", "label": "HAM", "hint": "使用 HAM 客户案例的站级 Method 和匈牙利式风格",
        "prompt": (
            "编程风格选择为【HAM】。优先检索 ham.* 案例；复杂 FB 主体按固定 Method 顺序组织："
            "A_Init、B_Constants、C_FB_Calls、D_ERROR_Handling、E_Timers_Edge_Detection、"
            "步骤序列、Others、Outputs。变量使用 b/s/i/fb/arr/ton/rtrig 等 HAM 匈牙利式前缀，"
            "错误、警告和步骤超时分层封装。保留现有公开对象名。"
        ),
    },
]


def style_prompt(style_id: str) -> str:
    selected = next((s for s in CODE_STYLES if s["id"] == style_id), CODE_STYLES[0])
    return selected["prompt"]


def _mcp_mapping(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MCP {label} 必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError(f"MCP {label} 必须是 JSON 对象")
    if len(value) > 32:
        raise ValueError(f"MCP {label} 最多支持 32 项")
    result: dict[str, str] = {}
    for key, item in value.items():
        key = str(key).strip()
        item = str(item)
        if not key or len(key) > 128 or any(ord(ch) < 32 for ch in key):
            raise ValueError(f"MCP {label} 的键名无效")
        if len(item) > MCP_VALUE_MAX or "\x00" in item or "\r" in item or "\n" in item:
            raise ValueError(f"MCP {label} 的值过长或包含非法换行")
        result[key] = item
    return result


def _normalize_mcp_server(server: object, existing: dict | None = None) -> dict:
    if not isinstance(server, dict):
        raise ValueError("MCP 服务配置必须是对象")
    existing = existing or {}
    server_id = str(server.get("id") or existing.get("id") or uuid.uuid4().hex[:8]).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", server_id):
        raise ValueError("MCP 服务 ID 只能包含字母、数字、下划线和短横线")
    name = str(server.get("name") or existing.get("name") or "未命名 MCP").strip()
    if not name or len(name) > MCP_SERVER_NAME_MAX:
        raise ValueError(f"MCP 服务名称长度必须为 1..{MCP_SERVER_NAME_MAX}")
    transport = str(server.get("transport") or existing.get("transport") or "stdio").strip()
    if transport not in MCP_TRANSPORTS:
        raise ValueError("MCP 传输方式必须是 stdio 或 streamable_http")

    def text_field(key: str, limit: int = MCP_VALUE_MAX) -> str:
        raw = server.get(key) if key in server else existing.get(key)
        value = str(raw or "").strip()
        if len(value) > limit or "\x00" in value:
            raise ValueError(f"MCP {key} 过长或包含非法字符")
        return value

    command = text_field("command", 512)
    url = text_field("url", 2048)
    cwd = text_field("cwd", 1024)
    if transport == "stdio" and not command:
        raise ValueError("stdio MCP 必须填写启动命令")
    if transport == "streamable_http":
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Streamable HTTP MCP 必须填写 http/https URL")

    raw_args = server.get("args") if "args" in server else existing.get("args", [])
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP args 必须是 JSON 数组") from exc
    if not isinstance(raw_args, list) or len(raw_args) > 64:
        raise ValueError("MCP args 必须是最多 64 项的 JSON 数组")
    args = []
    for item in raw_args:
        item = str(item)
        if len(item) > MCP_VALUE_MAX or "\x00" in item:
            raise ValueError("MCP args 含有过长或非法参数")
        args.append(item)

    raw_env = server.get("env") if "env" in server else existing.get("env", {})
    raw_headers = server.get("headers") if "headers" in server else existing.get("headers", {})
    env = _mcp_mapping(raw_env, "env")
    headers = _mcp_mapping(raw_headers, "headers")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env):
        raise ValueError("MCP env 的键名必须是合法环境变量名")
    timeout = server.get("timeout_seconds") if "timeout_seconds" in server else existing.get("timeout_seconds", 15)
    try:
        timeout = max(5, min(120, int(timeout)))
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP 超时时间必须是 5..120 秒") from exc
    return {
        "id": server_id,
        "name": name,
        "transport": transport,
        "enabled": bool(server.get("enabled", existing.get("enabled", True))),
        "command": command,
        "args": args,
        "cwd": cwd,
        "env": env,
        "url": url,
        "headers": headers,
        "timeout_seconds": timeout,
    }


def normalize_mcp_server(server: object) -> dict:
    """Validate a user-supplied MCP server without starting it."""
    return _normalize_mcp_server(server)


def sdk_permission_mode(perm_mode: str) -> str:
    for m in PERM_MODES:
        if m["id"] == perm_mode:
            return m["sdk"]
    return "default"


def _migrate(data: dict) -> dict:
    """Bring any prior config shape up to the provider-list model.

    产品版一律客户自带 Key —— 不再有'登录'档:迁移时把任何 kind=login 的档移除；
    active 若无有效指向则留空，避免保存配置时偷偷启用另一个档。"""
    if isinstance(data.get("providers"), list) and data["providers"]:
        cfg = {
            "active_provider": data.get("active_provider") or "",
            "providers": list(data["providers"]),
            "perm_mode": data.get("perm_mode", "ask"),
            "quality_gate_enabled": bool(data.get("quality_gate_enabled", True)),
            "code_style": data.get("code_style", "project"),
            "language": data.get("language", "zh"),
            "mcp_servers": data.get("mcp_servers", []),
        }
    else:
        # Legacy single-profile shape (auth_mode/api_key/base_url/model).
        providers = []
        active = ""
        if data.get("api_key"):
            providers.append({"id": "custom", "name": "自定义 API", "kind": "api",
                              "base_url": data.get("base_url", ""), "api_key": data["api_key"],
                              "model": data.get("model", "")})
            active = "custom"
        cfg = {"active_provider": active, "providers": providers,
               "perm_mode": data.get("perm_mode", "ask"),
               "quality_gate_enabled": bool(data.get("quality_gate_enabled", True)),
               "code_style": data.get("code_style", "project"),
               "language": data.get("language", "zh"),
               "mcp_servers": data.get("mcp_servers", [])}

    # 移除遗留的'登录'档(需自带 Key,该档无法使用)。
    cfg["providers"] = [p for p in cfg["providers"] if p.get("kind") != "login"]
    # Normalize every provider record.
    for p in cfg["providers"]:
        p.setdefault("kind", "api")
        p.setdefault("base_url", "")
        p.setdefault("api_key", "")
        p.setdefault("model", "")
        p.setdefault("protocol", "auto")
        # Old builds suggested DeepSeek's Anthropic compatibility endpoint.
        # Move official profiles to the OpenAI-compatible endpoint so V4's
        # reasoning_content can be handled and replayed correctly.
        if p["base_url"].rstrip("/").lower() == "https://api.deepseek.com/anthropic":
            p["base_url"] = "https://api.deepseek.com"
        p.setdefault("proxy", "")        # 可选 HTTP 代理;空 = 直连(不继承系统代理)
        p.setdefault("vision", False)    # 该模型是否多模态(能读图/PDF);决定附件是否发视觉块
        # V4 defaults to high-effort thinking upstream. Keep existing profiles
        # responsive unless the user explicitly enables thinking in settings.
        if "thinking" not in p:
            is_deepseek_v4 = (
                "api.deepseek.com" in p["base_url"].lower()
                and p["model"].lower().startswith("deepseek-v4")
            )
            p["thinking"] = "off" if is_deepseek_v4 else "auto"
        elif p["thinking"] not in ("auto", "off", "on"):
            p["thinking"] = "auto"
        if not p.get("id"):
            p["id"] = uuid.uuid4().hex[:8]
    # 保存 Provider 不等于启用 Provider。active 指向不存在（或原来是
    # login）时必须留空，交给用户显式点击“启用”，不能静默改用第一个档。
    if not any(p["id"] == cfg["active_provider"] for p in cfg["providers"]):
        cfg["active_provider"] = ""
    if cfg.get("code_style") not in {s["id"] for s in CODE_STYLES}:
        cfg["code_style"] = "project"
    if cfg.get("language") not in {"zh", "en"}:
        cfg["language"] = "zh"
    normalized_mcp = []
    for item in cfg.get("mcp_servers") or []:
        try:
            normalized_mcp.append(_normalize_mcp_server(item))
        except ValueError:
            # A malformed optional server must not make Provider settings
            # unreadable; the UI can still remove it after a migration.
            continue
    cfg["mcp_servers"] = normalized_mcp
    return cfg


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return _migrate(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Only the production default participates in legacy migration. Tests and
    # embedders that explicitly replace CONFIG_PATH must remain isolated.
    if CONFIG_PATH == DEFAULT_CONFIG_PATH and LEGACY_CONFIG_PATH.exists():
        try:
            cfg = _migrate(json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8")))
            return _write(cfg)
        except Exception:
            pass
    return _migrate({})


def _write(cfg: dict) -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def save_config(patch: dict) -> dict:
    """Merge top-level scalar settings into config."""
    cfg = load_config()
    for k in ("active_provider", "perm_mode", "quality_gate_enabled", "code_style", "language"):
        if k in patch and patch[k] is not None:
            cfg[k] = patch[k]
    return _write(cfg)


def set_active(provider_id: str) -> dict:
    cfg = load_config()
    if any(p["id"] == provider_id for p in cfg["providers"]):
        cfg["active_provider"] = provider_id
    return _write(cfg)


def upsert_provider(provider: dict) -> dict:
    """Add or update an API provider. An empty/absent api_key on an existing
    provider keeps the stored key. The login profile is not editable here."""
    cfg = load_config()
    pid = provider.get("id")
    rec = {
        "name": (provider.get("name") or "未命名").strip(),
        "kind": "api",
        "base_url": (provider.get("base_url") or "").strip(),
        "model": (provider.get("model") or "").strip(),
        "protocol": provider.get("protocol", "auto"),
        "proxy": (provider.get("proxy") or "").strip(),
        "vision": bool(provider.get("vision")),
        "thinking": provider.get("thinking")
                    if provider.get("thinking") in ("auto", "off", "on") else "auto",
    }
    if rec["protocol"] not in ("auto", "openai", "anthropic", "responses"):
        raise ValueError("不支持的接口协议")
    existing = next((p for p in cfg["providers"] if p["id"] == pid and p.get("kind") != "login"), None)
    if existing and "protocol" not in provider:
        rec["protocol"] = existing.get("protocol", "auto")
    if existing:
        key = provider.get("api_key")
        existing.update(rec)
        if key:  # empty means "keep stored key"
            existing["api_key"] = key
    else:
        rec["id"] = uuid.uuid4().hex[:8]
        rec["api_key"] = provider.get("api_key") or ""
        cfg["providers"].append(rec)
        # A newly saved profile stays inactive until the user explicitly
        # selects its independent “启用” action.
    return _write(cfg)


def delete_provider(provider_id: str) -> dict:
    cfg = load_config()
    if provider_id == LOGIN_ID:
        return cfg  # never delete the built-in login profile
    cfg["providers"] = [p for p in cfg["providers"] if p["id"] != provider_id]
    if cfg["active_provider"] == provider_id:
        # Do not silently fall through to another saved profile.  The next
        # request will receive a clear “no active Provider” diagnostic until
        # the user explicitly enables one.
        cfg["active_provider"] = ""
    return _write(cfg)


def upsert_mcp_server(server: dict) -> dict:
    cfg = load_config()
    server_id = str(server.get("id") or "").strip()
    existing = next((item for item in cfg["mcp_servers"] if item.get("id") == server_id), None)
    # Empty secret fields from the UI mean “keep existing”, just like API Key.
    candidate = dict(server)
    for key in ("env", "headers"):
        if key not in candidate and existing is not None:
            candidate[key] = existing.get(key, {})
    normalized = _normalize_mcp_server(candidate, existing)
    if existing:
        cfg["mcp_servers"] = [normalized if item.get("id") == normalized["id"] else item
                               for item in cfg["mcp_servers"]]
    else:
        cfg["mcp_servers"].append(normalized)
    return _write(cfg)


def set_mcp_server_enabled(server_id: str, enabled: bool) -> dict:
    cfg = load_config()
    for item in cfg["mcp_servers"]:
        if item.get("id") == server_id:
            item["enabled"] = bool(enabled)
            break
    return _write(cfg)


def delete_mcp_server(server_id: str) -> dict:
    cfg = load_config()
    cfg["mcp_servers"] = [item for item in cfg["mcp_servers"] if item.get("id") != server_id]
    return _write(cfg)


def active_env(cfg: dict | None = None) -> dict:
    """Return {"env": {...}, "model": "..."} for the active provider.

    no active provider -> empty env/model; the backend reports a clear error.
    api    -> ANTHROPIC_API_KEY (+ ANTHROPIC_BASE_URL) env + the profile's model.
    """
    cfg = cfg or load_config()
    prov = next((p for p in cfg["providers"] if p["id"] == cfg.get("active_provider")), None)
    if prov is None:
        return {"env": {}, "model": ""}
    if prov.get("kind") == "login":
        return {"env": {}, "model": prov.get("model") or ""}
    env: dict[str, str] = {}
    if prov.get("api_key"):
        env["ANTHROPIC_API_KEY"] = prov["api_key"]
    if prov.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = prov["base_url"]
    return {"env": env, "model": prov.get("model") or ""}


def public_settings(cfg: dict | None = None) -> dict:
    """Settings safe to send to the UI — API keys are never included."""
    cfg = cfg or load_config()
    providers = [{
        "id": p["id"], "name": p.get("name", ""), "kind": p.get("kind", "api"),
        "base_url": p.get("base_url", ""), "model": p.get("model", ""),
        "proxy": p.get("proxy", ""), "vision": bool(p.get("vision")),
        "thinking": p.get("thinking", "auto"),
        "protocol": p.get("protocol", "auto"),
        "has_key": bool(p.get("api_key")),
    } for p in cfg["providers"]]
    mcp_servers = [{
        "id": p["id"], "name": p.get("name", ""), "transport": p.get("transport", "stdio"),
        "enabled": bool(p.get("enabled", True)), "command": p.get("command", ""),
        "args": list(p.get("args") or []), "cwd": p.get("cwd", ""), "url": p.get("url", ""),
        "timeout_seconds": int(p.get("timeout_seconds") or 15),
        "has_env": bool(p.get("env")), "has_headers": bool(p.get("headers")),
    } for p in cfg.get("mcp_servers", [])]
    return {
        "active_provider": cfg.get("active_provider", ""),
        "providers": providers,
        "provider_templates": PROVIDER_TEMPLATES,
        "models": MODELS,
        "perm_mode": cfg.get("perm_mode", "ask"),
        "perm_modes": PERM_MODES,
        "quality_gate_enabled": bool(cfg.get("quality_gate_enabled", True)),
        "code_style": cfg.get("code_style", "project"),
        "code_styles": [{k: s[k] for k in ("id", "label", "hint")} for s in CODE_STYLES],
        "language": cfg.get("language", "zh"),
        "languages": [{"id": "zh", "label": "中文"}, {"id": "en", "label": "English"}],
        "mcp_transports": [{"id": key, "label": value} for key, value in MCP_TRANSPORTS.items()],
        "mcp_servers": mcp_servers,
    }
