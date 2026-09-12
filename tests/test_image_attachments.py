"""Offline image persistence and provider replay regressions. No PLC/network calls."""
import base64
import json
from pathlib import Path

import pytest

from tc_agent import attachments, agent_core
from tc_agent.conversation_store import ConversationStore, ConversationHistory
from tc_agent.openai_responses import ResponsesProvider

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aYZkAAAAASUVORK5CYII='


def uploaded(store, thread):
    upload = {'name': '截图.png', 'mime': 'image/png', 'data_b64': PNG}
    result = attachments.process([upload], vision=True)
    meta = store.save_images([upload], result['meta'])
    history = ConversationHistory(store, thread)
    history.add_event({'type': 'user', 'text': '按图绘制界面', 'attachments': meta})
    history.add_message({'role': 'user', 'text': '按图绘制界面'})
    return meta[0]['attachment_id']


def test_reopen_compaction_and_next_turn(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    image_id = uploaded(store, tid)
    history = ConversationHistory(store, tid)
    history.messages = [{'role': 'user', 'text': '为什么与原图不同？'}]
    history.summary = '已经生成 HMI 页面，需要对照图片验证。'
    history._save()
    reopened = ConversationStore(store.db_path)
    assert reopened.read_image(tid, image_id)['data_b64'] == PNG
    images, note = reopened.recent_images(tid)
    assert images[0]['data_b64'] == PNG
    assert '按图绘制界面' in note
    assert PNG not in json.dumps(reopened.load_state(tid))


def test_isolation_missing_and_corruption(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    image_id = uploaded(store, tid)
    other = store.create_thread('unrelated')['id']
    with pytest.raises(ValueError):
        store.read_image(other, image_id)
    with pytest.raises(ValueError):
        store.read_image(tid, '../../config.json')
    second = ConversationStore(tmp_path / 'other' / 'agent.db')
    with pytest.raises((ValueError, KeyError)):
        second.read_image(second.list_threads()[0]['id'], image_id)
    with store._connect() as db:
        db.execute('UPDATE attachments SET data=? WHERE id=?', (b'corrupt', image_id))
    assert store.recent_images(tid)[0] == []
    assert '不可读取' in store.recent_images(tid)[1]


def test_legacy_does_not_fall_back_to_wrong_image(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    uploaded(store, tid)
    ConversationHistory(store, tid).add_event({'type': 'user', 'text': '另一个设计',
        'attachments': [{'name': 'old.png', 'kind': 'image'}]})
    images, note = store.recent_images(tid)
    assert images == []
    assert 'old.png' in note and '重新上传' in note


def test_image_batch_transaction_rolls_back(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    uploads = [{'name': 'a.png', 'data_b64': PNG}, {'name': 'b.png', 'data_b64': '!!'}]
    meta = [{'kind': 'image', 'name': a['name']} for a in uploads]
    with pytest.raises(ValueError):
        store.save_images(uploads, meta)
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM attachments').fetchone()[0] == 0


def test_backup_includes_original_and_clear_revokes_access(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    image_id = uploaded(store, tid)
    backup = ConversationStore(Path(store.backup()['backup']))
    assert backup.read_image(tid, image_id)['data_b64'] == PNG
    ConversationHistory(store, tid).clear()
    with pytest.raises(ValueError):
        store.read_image(tid, image_id)
    assert store.recent_images(tid) == ([], '')


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_provider_sees_image_after_tools_and_followup(tmp_path, protocol):
    store = ConversationStore(tmp_path / 'agent.db')
    tid = store.list_threads()[0]['id']
    uploaded(store, tid)
    images, _ = store.recent_images(tid)
    history = [{'role': 'user', 'text': '按图绘制'},
               {'role': 'assistant', 'text': '', 'tool_calls': [{'id': 'c1', 'name': 'inspect', 'args': {}}]},
               {'role': 'tool', 'id': 'c1', 'result': {'ok': True}}]
    for followup in (False, True):
        if followup:
            history += [{'role': 'assistant', 'text': '已绘制'}, {'role': 'user', 'text': '再对照原图'}]
        if protocol == 'responses':
            wire = ResponsesProvider('https://api.openai.com/v1', 'fixture', 'fixture')._body('', history, [], visual=images)
        elif protocol == 'openai':
            wire = agent_core.OpenAIProvider('https://example.invalid', 'fixture', 'fixture')._messages('', history)
            agent_core._openai_attach_visual(wire, images)
        else:
            wire = agent_core.AnthropicProvider('https://example.invalid', 'fixture', 'fixture')._messages(history)
            agent_core._anthropic_attach_visual(wire, images)
        assert PNG in json.dumps(wire)


def test_backend_keeps_visuals_after_first_step():
    source = (Path(__file__).resolve().parents[1] / 'tc_agent/backend.py').read_text(encoding='utf-8')
    assert 'visual=turn_visual if step == 0 else None' not in source
    assert 'conversation_store.recent_images, active_thread_id' in source
    assert 'data.get("thread_id") != active_thread_id' in source
