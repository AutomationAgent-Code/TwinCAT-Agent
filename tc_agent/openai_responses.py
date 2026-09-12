"""Stateless Responses adapter; local history owns every completed output item.

No server-side conversation, implicit tool execution or cross-provider state.
Only a completed response with valid calls may enter the Agent execution loop.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from .agent_core import Provider, _ensure_tool_results, _post_json, _post_sse


def responses_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.query or parts.fragment:
        raise ValueError('Responses Base URL 必须是无查询参数的 HTTP(S) 地址')
    if parts.username or parts.password:
        raise ValueError('Base URL 不允许包含凭据，请使用 API Key 字段')
    if parts.hostname == 'api.openai.com' and parts.scheme != 'https':
        raise ValueError('OpenAI 官方 API 必须使用 HTTPS')
    path = parts.path.rstrip('/')
    for suffix in ('/responses', '/chat/completions'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path and parts.hostname == 'api.openai.com':
        path = '/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


class ResponsesProvider(Provider):
    protocol = 'responses'

    def __init__(self, base_url, api_key, model, proxy=None, thinking='auto'):
        super().__init__(responses_base_url(base_url), api_key, model, proxy, thinking)
        # Encrypted state is reusable only under the same endpoint/model/key.
        self.scope = hashlib.sha256(json.dumps(
            [self.base_url, self.model, self.api_key]).encode()).hexdigest()

    def _input(self, history):
        items, pending = [], set()
        for message in _ensure_tool_results(history):
            role = message.get('role')
            if role == 'user':
                items.append({'role': 'user', 'content': message.get('text') or ''})
            elif role == 'assistant':
                state = message.get('responses_state') or {}
                if state.get('scope') == self.scope and isinstance(state.get('output'), list):
                    items.extend(deepcopy(state['output']))
                else:
                    if message.get('text'):
                        items.append({'role': 'assistant', 'content': message['text']})
                    for call in message.get('tool_calls') or []:
                        items.append({'type': 'function_call', 'call_id': call['id'],
                                      'name': call['name'],
                                      'arguments': json.dumps(call['args'], ensure_ascii=False)})
                pending.update(call['id'] for call in message.get('tool_calls') or [])
            elif role == 'tool' and message.get('id') in pending:
                items.append({'type': 'function_call_output', 'call_id': message['id'],
                              'output': json.dumps(message.get('result'), ensure_ascii=False)})
                pending.remove(message['id'])
        return items

    def _body(self, system, history, tools, stream=False, visual=None):
        items = self._input(history)
        if visual:
            parts = []
            for block in visual:
                data = f"data:{block['media_type']};base64,{block['data_b64']}"
                if block.get('kind') == 'image':
                    parts.append({'type': 'input_image', 'image_url': data})
                elif block.get('kind') == 'pdf':
                    parts.append({'type': 'input_file', 'filename': block.get('name') or 'attachment.pdf',
                                  'file_data': data})
            for item in reversed(items):
                if item.get('role') == 'user':
                    item['content'] = [{'type': 'input_text', 'text': item['content']}] + parts
                    break
        body = {'model': self.model, 'instructions': system, 'input': items,
                'store': False, 'include': ['reasoning.encrypted_content'], 'stream': stream}
        if tools:
            # Existing PLC schemas use omitted optional fields (not null).
            # Preserve those semantics; execution still passes the normal gate.
            body.update(tools=[{'type': 'function', 'name': t['name'],
                                'description': t['description'], 'parameters': deepcopy(t['parameters']),
                                'strict': False} for t in tools], tool_choice='auto',
                        parallel_tool_calls=False)
        if self.thinking != 'auto':
            # Codex does not support disabling reasoning; off means low effort.
            body['reasoning'] = {'effort': 'low' if self.thinking == 'off' else 'high'}
        return body

    def _parse(self, response):
        if not isinstance(response, dict) or response.get('status') != 'completed' or response.get('error'):
            error = response.get('error') or response.get('incomplete_details') if isinstance(response, dict) else None
            raise RuntimeError('OpenAI Responses 未完成，未执行任何工具：' + str(error or '缺少 completed 状态')[:600])
        output = response.get('output')
        if not isinstance(output, list):
            raise RuntimeError('OpenAI Responses 缺少完整 output，未执行任何工具')
        texts, calls, ids = [], [], set()
        for item in output:
            kind = item.get('type')
            if kind == 'message':
                if item.get('status') not in (None, 'completed'):
                    raise RuntimeError('OpenAI Responses 消息不完整')
                for part in item.get('content') or []:
                    if part.get('type') == 'output_text':
                        texts.append(part.get('text') or '')
                    elif part.get('type') == 'refusal':
                        texts.append(part.get('refusal') or '')
            elif kind == 'function_call':
                call_id, name = item.get('call_id'), item.get('name')
                if not call_id or not name or call_id in ids or item.get('status') not in (None, 'completed'):
                    raise RuntimeError('OpenAI Responses 工具调用 ID/名称/完成状态无效')
                try:
                    args = json.loads(item.get('arguments') or '', parse_constant=self._invalid_constant)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError(f'OpenAI Responses 工具 {name} 参数不是完整 JSON，未执行') from exc
                if not isinstance(args, dict):
                    raise RuntimeError(f'OpenAI Responses 工具 {name} 参数必须是对象，未执行')
                ids.add(call_id)
                calls.append({'id': call_id, 'name': name, 'args': args})
            elif kind != 'reasoning':
                raise RuntimeError(f'OpenAI Responses 返回未启用的输出类型：{kind}')
        usage = response.get('usage') or {}
        return {'text': ''.join(texts), 'tool_calls': calls, 'reasoning_content': '',
                'usage': {'in': usage.get('input_tokens', 0), 'out': usage.get('output_tokens', 0)},
                'responses_state': {'scope': self.scope, 'output': deepcopy(output)}}

    @staticmethod
    def _invalid_constant(value):
        raise ValueError(f'Invalid JSON constant: {value}')

    def complete(self, system, history, tools, extra_user_content=None):
        response = _post_json(self.base_url + '/responses',
                              {'Authorization': f'Bearer {self.api_key}'},
                              self._body(system, history, tools, visual=extra_user_content),
                              proxy=self.proxy)
        return self._parse(response)

    def complete_stream(self, system, history, tools, on_delta,
                        extra_user_content=None, on_activity=None):
        stream = _post_sse(self.base_url + '/responses',
                           {'Authorization': f'Bearer {self.api_key}'},
                           self._body(system, history, tools, stream=True, visual=extra_user_content),
                           proxy=self.proxy)
        displayed = ''
        try:
            for event, chunk in stream:
                kind = chunk.get('type') or event
                if kind == 'response.completed':
                    step = self._parse(chunk.get('response'))
                    if not step['text'].startswith(displayed):
                        raise RuntimeError('OpenAI Responses 流式正文与最终正文不一致，未执行工具')
                    if step['text'][len(displayed):]:
                        on_delta(step['text'][len(displayed):])
                    return step
                if kind in ('error', 'response.failed', 'response.incomplete'):
                    detail = chunk.get('response') or chunk
                    raise RuntimeError('OpenAI Responses 调用失败：' + str(
                        detail.get('error') or detail.get('incomplete_details') or detail.get('message') or kind)[:600])
                if kind in ('response.output_text.delta', 'response.refusal.delta'):
                    delta = chunk.get('delta') or ''
                    displayed += delta
                    if delta:
                        on_delta(delta)
                elif on_activity:
                    on_activity('reasoning' if 'reasoning' in (kind or '') else 'working')
            # Never execute arguments from a disconnected/partial SSE stream.
            raise RuntimeError('OpenAI Responses 流中断，未收到 response.completed；未执行工具')
        finally:
            close = getattr(stream, 'close', None)
            if close:
                close()
