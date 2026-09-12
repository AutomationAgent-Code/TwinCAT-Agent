from copy import deepcopy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

import pytest

from tc_agent import agent_core as ac, backend, config, openai_responses as wire
from tc_agent.conversation_store import ConversationStore


TOOLS = [{'name': 'plc_read', 'description': 'Read PLC',
          'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}},
                         'required': ['name']}}]
USER = {'role': 'user', 'text': '读取 MAIN'}


def provider(**kw):
    return wire.ResponsesProvider(kw.pop('url', 'https://api.openai.com/v1'),
                                  kw.pop('key', 'fake-test-key'), kw.pop('model', 'gpt-5.3-codex'), **kw)


def response(calls=True):
    output = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'opaque'},
              {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': '已读取', 'annotations': []}]}]
    if calls:
        output.append({'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                       'name': 'plc_read', 'arguments': '{"name":"MAIN"}', 'status': 'completed'})
    return {'id': 'resp_1', 'status': 'completed', 'output': output,
            'usage': {'input_tokens': 100, 'output_tokens': 23}}


def test_body_preserves_optional_schema_and_stateless_protocol():
    body = provider()._body('system', [USER], TOOLS)
    assert body['store'] is False
    assert body['include'] == ['reasoning.encrypted_content']
    assert body['tools'][0]['strict'] is False
    assert body['tools'][0]['parameters'] == TOOLS[0]['parameters']
    assert body['parallel_tool_calls'] is False
    assert body['instructions'] == 'system'
    assert not {'messages', 'previous_response_id', 'conversation', 'stream_options', 'reasoning'} & body.keys()


@pytest.mark.parametrize('thinking,effort', [('off', 'low'), ('on', 'high')])
def test_effort(thinking, effort):
    assert provider(thinking=thinking)._body('', [USER], [])['reasoning'] == {'effort': effort}


@pytest.mark.parametrize('suffix', ['', '/', '/v1', '/v1/', '/v1/responses', '/v1/chat/completions'])
def test_official_base_url(suffix):
    assert provider(url='https://api.openai.com' + suffix).base_url == 'https://api.openai.com/v1'


@pytest.mark.parametrize('url', ['', 'api.openai.com', 'https://api.openai.com/v1?key=x',
                                 'http://api.openai.com/v1', 'https://user:pass@host/v1'])
def test_invalid_urls_fail_before_request(url):
    with pytest.raises(ValueError):
        provider(url=url)


def test_complete_request_and_tool_pairing_after_durable_reload(tmp_path):
    p = provider()
    with patch.object(wire, '_post_json', return_value=response()) as post:
        step = p.complete('system', [USER], TOOLS)
    assert post.call_args.args[0] == 'https://api.openai.com/v1/responses'
    assert step['tool_calls'][0]['id'] == 'call_1'  # NOT the fc_ item ID
    assert step['usage'] == {'in': 100, 'out': 23}
    assert step['reasoning_content'] == ''
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    history = [USER, ac.assistant_message(step),
               {'role': 'tool', 'id': 'call_1', 'result': {'declaration': 'PROGRAM MAIN'}}]
    store.save_state(tid, {'messages': history})
    restored = ConversationStore(tmp_path / 'agent.db').load_state(tid)['messages']
    items = provider()._input(restored)
    assert items[1:4] == response()['output']
    assert items[4]['call_id'] == 'call_1'
    assert items[4]['type'] == 'function_call_output'
    assert json.loads(items[4]['output']) == {'declaration': 'PROGRAM MAIN'}
    assert len([i for i in items if i.get('type') == 'function_call']) == 1


@pytest.mark.parametrize('change', [{'model': 'another-model'}, {'key': 'another-key'}, {'url': 'https://other.example/v1'}])
def test_no_opaque_state_cross_provider(change):
    history = [USER, ac.assistant_message(provider()._parse(response()))]
    items = provider(**change)._input(history)
    assert not any(i.get('type') == 'reasoning' for i in items)
    assert any(i.get('type') == 'function_call' for i in items)
    assert items[-1]['type'] == 'function_call_output'  # stopped call placeholder


def test_legacy_history_orphan_tool_and_cancelled_calls():
    h = [USER, {'role': 'tool', 'id': 'orphan', 'result': 'do not replay'},
         {'role': 'assistant', 'text': '', 'tool_calls': [{'id': 'old', 'name': 'plc_read', 'args': {}}]}]
    items = provider()._input(h)
    assert not any(i.get('call_id') == 'orphan' for i in items)
    assert json.loads(items[-1]['output']).get('aborted')


def test_visual_blocks_do_not_mutate_persisted_history():
    history = [deepcopy(USER)]
    blocks = [{'kind': 'image', 'media_type': 'image/png', 'data_b64': 'AA=='},
              {'kind': 'pdf', 'media_type': 'application/pdf', 'data_b64': 'BB==', 'name': 'PLC.pdf'}]
    body = provider()._body('s', history, [], visual=blocks)
    content = body['input'][0]['content']
    assert [p['type'] for p in content] == ['input_text', 'input_image', 'input_file']
    assert content[2]['filename'] == 'PLC.pdf'
    assert history == [USER]
    assert 'tools' not in body and 'tool_choice' not in body


def test_sse_only_completed_response_is_authoritative():
    events = [{'type': 'response.created'},
              {'type': 'response.reasoning_summary_text.delta', 'delta': 'never show this'},
              {'type': 'response.function_call_arguments.delta', 'delta': '{"na'},
              {'type': 'response.function_call_arguments.delta', 'delta': 'me":"WRONG"}'},
              {'type': 'response.output_text.delta', 'delta': '已'},
              {'type': 'response.completed', 'response': response()}]
    text, activity = [], []
    with patch.object(wire, '_post_sse', return_value=iter((None, e) for e in events)):
        step = provider().complete_stream('s', [USER], TOOLS, text.append, on_activity=activity.append)
    assert ''.join(text) == '已读取'
    assert step['tool_calls'][0]['args'] == {'name': 'MAIN'}
    assert 'reasoning' in activity


@pytest.mark.parametrize('kind', ['error', 'response.failed', 'response.incomplete', 'eof'])
def test_partial_or_failed_stream_never_returns_calls(kind):
    events = [(None, {'type': 'response.function_call_arguments.done', 'arguments': '{}'})]
    if kind != 'eof':
        events.append((None, {'type': kind, 'error': {'message': 'fixture failure'}}))
    with patch.object(wire, '_post_sse', return_value=iter(events)), pytest.raises(RuntimeError):
        provider().complete_stream('s', [USER], TOOLS, lambda _: None)


@pytest.mark.parametrize('args', ['', '{', '[]', 'null', '{"x":NaN}', 'true'])
def test_invalid_arguments_fail_closed(args):
    r = response()
    r['output'][-1]['arguments'] = args
    with pytest.raises(RuntimeError):
        provider()._parse(r)


def test_duplicate_calls_and_incomplete_output_fail_closed():
    r = response()
    r['output'].append(deepcopy(r['output'][-1]))
    with pytest.raises(RuntimeError):
        provider()._parse(r)
    r = response()
    r['status'] = 'incomplete'
    with pytest.raises(RuntimeError):
        provider()._parse(r)


def test_cancellation_callback_closes_stream():
    closed = []
    def events():
        try:
            yield None, {'type': 'response.output_text.delta', 'delta': 'x'}
            yield None, {'type': 'response.completed', 'response': response()}
        finally:
            closed.append(True)
    def cancelled(_):
        raise RuntimeError('cancelled')
    with patch.object(wire, '_post_sse', return_value=events()), pytest.raises(RuntimeError, match='cancelled'):
        provider().complete_stream('', [USER], TOOLS, cancelled)
    assert closed == [True]


@pytest.mark.parametrize('url,protocol,expected', [
    ('https://api.openai.com/v1', 'auto', 'responses'),
    ('https://api.openai.com.evil.example/v1', 'auto', 'openai'),
    ('https://api.deepseek.com', 'auto', 'openai'),
    ('', 'auto', 'anthropic'),
    ('https://api.openai.com/v1', 'openai', 'openai'),
    ('https://gateway.example/v1', 'responses', 'responses'),
])
def test_backend_provider_routing(url, protocol, expected):
    p, error = backend._build_provider({'active_provider': 'p', 'providers': [
        {'id': 'p', 'api_key': 'test-key', 'model': 'test-model', 'base_url': url, 'protocol': protocol}]})
    assert not error
    assert p.protocol == expected


def test_config_roundtrip_keeps_protocol_and_hides_key(tmp_path):
    with patch.object(config, 'CONFIG_PATH', tmp_path / 'config.json'):
        saved = config.upsert_provider({'name': 'official', 'base_url': 'https://api.openai.com/v1',
                                        'api_key': 'test-secret', 'model': 'gpt-5.3-codex', 'protocol': 'responses'})
        public = config.public_settings(saved)
        assert public['providers'][0]['protocol'] == 'responses'
        assert 'test-secret' not in json.dumps(public)
        saved = config.upsert_provider({'id': saved['providers'][0]['id'], 'name': 'edited', 'api_key': ''})
        assert saved['providers'][0]['protocol'] == 'responses'
        assert saved['providers'][0]['api_key'] == 'test-secret'


def test_config_migrates_legacy_file_to_stable_user_path_once(tmp_path):
    legacy = tmp_path / 'legacy' / 'config.json'
    stable = tmp_path / 'user-data' / 'config.json'
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({
        'active_provider': 'p',
        'providers': [{'id': 'p', 'name': 'kept', 'kind': 'api',
                       'api_key': 'secret', 'model': 'model'}],
    }), encoding='utf-8')
    with patch.object(config, 'CONFIG_PATH', stable), \
         patch.object(config, 'DEFAULT_CONFIG_PATH', stable), \
         patch.object(config, 'LEGACY_CONFIG_PATH', legacy):
        loaded = config.load_config()
        assert loaded['active_provider'] == 'p'
        assert stable.is_file()
        legacy.write_text(json.dumps({'providers': []}), encoding='utf-8')
        assert config.load_config()['providers'][0]['api_key'] == 'secret'


def test_explicit_config_path_does_not_import_module_legacy_file(tmp_path):
    isolated = tmp_path / 'isolated' / 'config.json'
    legacy = tmp_path / 'legacy.json'
    legacy.write_text(json.dumps({'providers': [{'id': 'wrong'}]}), encoding='utf-8')
    with patch.object(config, 'CONFIG_PATH', isolated), \
         patch.object(config, 'DEFAULT_CONFIG_PATH', tmp_path / 'production.json'), \
         patch.object(config, 'LEGACY_CONFIG_PATH', legacy):
        assert config.load_config()['providers'] == []


def test_real_local_http_sse_and_json_transport():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, body))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body['stream'] else 'application/json')
            self.end_headers()
            if body['stream']:
                for event in [{'type': 'response.output_text.delta', 'delta': '已读取'},
                              {'type': 'response.completed', 'response': response(False)}]:
                    self.wfile.write(('event: ' + event['type'] + '\ndata: ' +
                                      json.dumps(event, ensure_ascii=False) + '\n\n').encode())
            else:
                self.wfile.write(json.dumps(response(False)).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        p = provider(url=f'http://127.0.0.1:{server.server_port}/v1')
        assert p.complete('s', [USER], [])['text'] == '已读取'
        text = []
        assert p.complete_stream('s', [USER], [], text.append)['text'] == '已读取'
        assert ''.join(text) == '已读取'
        assert all(path == '/v1/responses' for path, _ in received)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('mode,tool_name,executed', [('auto', 'plc_read', True), ('plan', 'plc_write', False)])
def test_agent_loop_keeps_existing_gate_and_returns_tool_results(mode, tool_name, executed):
    first = response()
    first['output'][-1]['name'] = tool_name
    if tool_name == 'plc_write':
        # Exercise the permission gate with a structurally valid request.
        first['output'][-1]['arguments'] = json.dumps({
            'name': 'MAIN', 'area': 'implementation', 'code': '',
        })
    second = response(False)
    with patch.object(wire, '_post_json', side_effect=[first, second]) as post, \
         patch.object(ac, 'run_tool', return_value={'status': 'read'}) as run:
        assert ac.run('test', provider(), mode=mode, verbose=False) == '已读取'
    assert run.called is executed
    next_body = post.call_args_list[1].args[2]
    assert next_body['input'][-1]['call_id'] == 'call_1'
    result = json.loads(next_body['input'][-1]['output'])
    assert ('denied' in result) is (not executed)
    assert next_body['input'][1]['encrypted_content'] == 'opaque'


def test_refusal_is_visible_without_tools():
    r = response(False)
    r['output'][1]['content'] = [{'type': 'refusal', 'refusal': '不能执行此操作'}]
    step = provider()._parse(r)
    assert step['text'] == '不能执行此操作'
    assert step['tool_calls'] == []
