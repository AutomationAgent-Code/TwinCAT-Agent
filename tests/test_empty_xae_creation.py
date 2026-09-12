import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tc_agent.backend import _detect_solution
from tc_agent.host_lease import HostLease, HostUnavailable
from tc_agent.conversation_store import ConversationStore
from tc_template.solution_creation import create_solution, template_catalog


def test_empty_xae_is_connected():
    with patch('tc_template._ps_bridge.ps_com', return_value={'pid': 42, 'solution': ''}):
        assert asyncio.run(_detect_solution(42, strict_pid=True)) == ('', 42, '')


def test_catalog_is_json_serializable():
    assert 'basic' in json.dumps(template_catalog())


def test_create_real_template_in_same_mock_host(tmp_path):
    expected = tmp_path / 'NewMachine' / 'NewMachine.sln'
    calls = []
    def com(command, **args):
        calls.append((command, args))
        if command == 'open-created-solution':
            assert expected.is_file()
            assert args['path'] == str(expected)
            return {'status': 'opened'}
        return {'pid': 42, 'solution': str(expected) if len(calls) > 1 else ''}
    with patch('tc_template._ps_bridge.ps_com', side_effect=com):
        result = create_solution('NewMachine', str(tmp_path), 'basic')
    assert result['verified'] and result['pid'] == 42
    assert [c[0] for c in calls] == ['connect-check', 'open-created-solution', 'connect-check']


def test_existing_solution_not_replaced(tmp_path):
    with patch('tc_template._ps_bridge.ps_com', return_value={'pid': 42, 'solution': 'existing.sln'}):
        with pytest.raises(ValueError):
            create_solution('NewMachine', str(tmp_path), 'basic')
    assert not (tmp_path / 'NewMachine').exists()


def test_existing_directory_not_overwritten(tmp_path):
    (tmp_path / 'Existing').mkdir()
    with pytest.raises(ValueError):
        create_solution('Existing', str(tmp_path), 'basic')


def test_native_empty_project_info_does_not_require_system_manager():
    from tc_template._native_bridge import _project_info
    dte = SimpleNamespace(Solution=SimpleNamespace(FullName='', Projects=SimpleNamespace(Count=0)))
    with patch('tc_template.tc_platform.get_project_info', side_effect=AssertionError('No System Manager')):
        assert _project_info(dte)['state'] == 'connected_no_project'


def test_native_creation_refuses_racing_user_open():
    from tc_template._native_bridge import _open_created_solution
    dte = SimpleNamespace(Solution=SimpleNamespace(IsOpen=True))
    with pytest.raises(RuntimeError):
        _open_created_solution(dte, 'new.sln')


def test_verified_creation_advances_lease(tmp_path):
    path = str(tmp_path / 'new.sln')
    with patch('tc_agent.host_lease.process_identity', return_value=(42, 1)), \
         patch('tc_template._ps_bridge.ps_com', return_value={'solution': path}):
        lease = HostLease(42, '')
        assert lease.adopt_created_solution({'pid': 42, 'verified': True, 'solution': path}) == path
        lease.check_context(42, path)


def test_changed_process_rejected_after_creation():
    with patch('tc_agent.host_lease.process_identity', side_effect=[(42, 1), (42, 2)]):
        lease = HostLease(42, '')
        with pytest.raises(HostUnavailable):
            lease.adopt_created_solution({'pid': 42, 'verified': True, 'solution': 'new.sln'})


def test_creation_context_and_images_survive_handoff(tmp_path):
    source = ConversationStore(tmp_path / 'source.db')
    destination = ConversationStore(tmp_path / 'destination.db')
    thread = source.create_thread('My creation')
    attachment = 'a' * 32
    raw = b'image evidence'
    with source._connect() as db:
        db.execute('INSERT INTO attachments VALUES(?,?,?,?,?)',
                   (attachment, 'drawing.png', 'image/png', hashlib.sha256(raw).hexdigest(), raw))
    source.save_state(thread['id'], {'messages': [{'role': 'user', 'text': 'create from image'}],
        'events': [{'type': 'user', 'attachments': [{'kind': 'image', 'attachment_id': attachment}]}],
        'summary': 'User intent', 'recovery': {'error': 'verification pending'}})
    source.set_status(thread['id'], 'incomplete')
    copied = source.copy_creation_context(thread['id'], destination)
    assert destination.load_state(copied['id']) == source.load_state(thread['id'])
    assert destination.read_image(copied['id'], attachment)['name'] == 'drawing.png'
    assert destination.claim_foreground(copied['id'])
