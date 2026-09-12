import unittest
from tc_agent.execution_policy import (
    request_action_blocks, request_action_scope, readonly_request_blocks,
)


class RuntimeRequestScopeTests(unittest.TestCase):
    def test_natural_language_never_vetoes_a_tool(self):
        for text in ('你读一下目标平台', '查看 PLC 状态', '检查为什么 tc_start 失败',
                     '读取 start 状态', '只看平台，不要切换', 'read the target platform',
                     '授权', '继续', '按方案来', '无关键词需求'):
            with self.subTest(text=text):
                self.assertFalse(readonly_request_blocks(text, False))
                self.assertFalse(readonly_request_blocks(text, True))
                self.assertFalse(request_action_blocks(text, 'plc_write', False))
                self.assertFalse(request_action_blocks(text, 'tc_start', False))

    def test_scope_metadata_no_longer_classifies_request_text(self):
        scope = request_action_scope('只读查看，不要启动或修改；历史中曾要求修复')
        self.assertEqual('normal', scope['mode'])
        self.assertEqual({'code', 'runtime'}, scope['allowed'])

    def test_backend_foreground_and_worker_do_not_call_deprecated_veto(self):
        from pathlib import Path
        text = (Path(__file__).parents[1] / 'tc_agent/backend.py').read_text(encoding='utf-8')
        self.assertNotIn('request_action_blocks(', text)
        self.assertNotIn('readonly_request_blocks(', text)
        self.assertIn('_tool_decide(mode, name)', text)
        self.assertIn('await approve(', text)
