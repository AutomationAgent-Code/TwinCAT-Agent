from __future__ import annotations

import unittest
from unittest.mock import patch

from tc_agent import agent_core, config


class DeepSeekCompatibilityTests(unittest.TestCase):
    def test_request_without_tools_omits_tool_choice(self) -> None:
        provider = agent_core.OpenAIProvider(
            "https://api.deepseek.com", "test-key", "deepseek-chat"
        )
        body = provider._request_body(
            [{"role": "user", "content": "summarize"}], [], stream=False
        )
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_template_uses_current_openai_endpoint_and_v4_models(self) -> None:
        template = next(t for t in config.PROVIDER_TEMPLATES if t["key"] == "deepseek")
        self.assertEqual("https://api.deepseek.com", template["base_url"])
        self.assertEqual("off", template["thinking"])
        self.assertIn("deepseek-v4-flash", template["models"])
        self.assertIn("deepseek-v4-pro", template["models"])

    def test_old_deepseek_profile_migrates_to_stable_v4_mode(self) -> None:
        cfg = config._migrate({
            "active_provider": "ds",
            "providers": [{
                "id": "ds",
                "kind": "api",
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "test-key",
                "model": "deepseek-v4-flash",
            }],
        })
        provider = cfg["providers"][0]
        self.assertEqual("https://api.deepseek.com", provider["base_url"])
        self.assertEqual("off", provider["thinking"])

    def test_v4_stable_mode_disables_thinking_in_request(self) -> None:
        provider = agent_core.OpenAIProvider(
            "https://api.deepseek.com", "test-key", "deepseek-v4-flash",
            thinking="off",
        )
        body = provider._request_body([], [], stream=True)
        self.assertEqual({"type": "disabled"}, body["thinking"])

        other = agent_core.OpenAIProvider(
            "https://example.com/v1", "test-key", "deepseek-v4-flash",
            thinking="off",
        )
        self.assertNotIn("thinking", other._request_body([], [], stream=True))

    def test_reasoning_stream_is_kept_for_followup_tool_call(self) -> None:
        provider = agent_core.OpenAIProvider(
            "https://api.deepseek.com", "test-key", "deepseek-v4-flash",
            thinking="on",
        )
        chunks = [
            ("message", {"choices": [{"delta": {"reasoning_content": "分析中"}}]}),
            ("message", {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "function": {"name": "tc_state", "arguments": "{}"},
            }]}}]}),
            ("message", {"choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 3,
            }}),
        ]
        activity: list[str] = []
        with patch.object(agent_core, "_post_sse", return_value=iter(chunks)):
            step = provider.complete_stream(
                "system", [{"role": "user", "text": "状态"}], [], lambda _text: None,
                on_activity=activity.append,
            )

        self.assertEqual("分析中", step["reasoning_content"])
        self.assertEqual(["reasoning"], activity)
        self.assertEqual("tc_state", step["tool_calls"][0]["name"])

        messages = provider._messages("system", [{
            "role": "assistant",
            "text": "",
            "reasoning_content": step["reasoning_content"],
            "tool_calls": step["tool_calls"],
        }, {
            "role": "tool", "id": "call_1", "name": "tc_state", "result": {"ok": True},
        }])
        self.assertEqual("分析中", messages[1]["reasoning_content"])


if __name__ == "__main__":
    unittest.main()
