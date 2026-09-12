from unittest.mock import patch

import pytest

from tc_agent import backend


def test_empty_history_is_not_global_or_project_history():
    with patch.dict(backend.EMPTY_XAE_SCOPES, {}, clear=True), \
         patch('tc_agent.host_lease.process_identity', side_effect=lambda pid: (pid, 11)):
        a = backend._empty_xae_scope(41)
        assert a == backend._empty_xae_scope(41)  # reconnect keeps draft
        assert a != backend._empty_xae_scope(42)
        path = backend._conversation_db_for('', a)
        assert path.parent.name == a
        assert path.parent.parent.name == '.empty_xae'
        backend._leave_empty_xae_scope(41)  # open a project, later close it
        assert a != backend._empty_xae_scope(41)


def test_pid_reuse_does_not_restore_previous_draft():
    with patch.dict(backend.EMPTY_XAE_SCOPES, {}, clear=True), \
         patch('tc_agent.host_lease.process_identity', side_effect=[(41, 11), (41, 12)]):
        assert backend._empty_xae_scope(41) != backend._empty_xae_scope(41)


def test_no_implicit_global_history_fallback():
    with pytest.raises(ValueError):
        backend._conversation_db_for('')
    assert backend._empty_xae_scope(0) != backend._empty_xae_scope(0)


def test_project_database_location_is_unchanged(tmp_path):
    assert backend._conversation_db_for(str(tmp_path / 'machine.sln')) == tmp_path / '.TwinCATAgent' / 'agent.db'


def test_unavailable_host_does_not_prevent_settings_connection():
    from tc_agent.host_lease import HostUnavailable
    with patch('tc_agent.host_lease.process_identity', side_effect=HostUnavailable('closed')):
        assert backend._empty_xae_scope(41) != backend._empty_xae_scope(41)
