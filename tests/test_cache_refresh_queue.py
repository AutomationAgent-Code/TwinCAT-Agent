import threading
import time
import json
import http.server
import urllib.request
from unittest.mock import Mock, patch

import pytest

from tc_agent.cache_refresh import CacheRefreshQueue, StaleCacheNotification, InactivePlcDocument
from tc_agent.plc_cache import PlcSourceCache, cache_scope
from tc_agent import backend


def payload(path='C:/PLC/MAIN.TcPOU', pid=12, solution='A.sln', member='', text='fresh'):
    return dict(path=path, xae_pid=pid, solution=solution, member=member,
                implementation=text, declaration='VAR END_VAR', saved=False)


def idle(queue):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        states = queue.snapshot()
        if states and all(s['status'] not in ('queued', 'reading') for s in states):
            return states
        time.sleep(.01)
    raise AssertionError(queue.snapshot())


def test_burst_and_duplicate_transports_read_only_latest():
    cache = PlcSourceCache()
    read = Mock(side_effect=lambda pid, sol, expected: payload(expected['path'], pid, sol))
    queue = CacheRefreshQueue(cache, read, delay=.03)
    for name in ['A', 'A', 'B', 'MAIN', 'MAIN']:
        assert queue.submit(12, 'A.sln', {'path': f'C:/PLC/{name}.TcPOU'})['status'] == 'queued'
    assert idle(queue)[0]['status'] == 'updated'
    assert read.call_count == 1
    assert read.call_args.args[2]['path'] == 'C:/PLC/MAIN.TcPOU'


def test_stale_notification_resyncs_actual_identity_without_clearing_other_cache():
    cache = PlcSourceCache()
    cache.put(payload('C:/Other/Keep.TcPOU', 99, 'Other.sln'))
    cache.put(payload('C:/PLC/MAIN.TcPOU', member='Run'))
    read = Mock(side_effect=[StaleCacheNotification('switched'), payload('C:/PLC/New.TcPOU')])
    queue = CacheRefreshQueue(cache, read, delay=.01)
    queue.submit(12, 'A.sln', {'path': 'C:/PLC/Old.TcPOU', 'member': 'Stop'})
    assert idle(queue)[0]['status'] == 'resynced'
    assert read.call_args.args == (12, 'A.sln', None)
    with cache_scope(99, 'Other.sln'):
        assert cache.get('Keep') is not None
    with cache_scope(12, 'A.sln'):
        assert cache.get('MAIN', 'Run') is not None
        assert cache.get('New') is not None
        assert cache.get('Old', 'Stop') is None


def test_inflight_old_read_never_overwrites_new_notification():
    cache, started, release = PlcSourceCache(), threading.Event(), threading.Event()
    calls = []
    def read(pid, sol, expected):
        calls.append(expected['path'])
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        return payload(expected['path'])
    queue = CacheRefreshQueue(cache, read, delay=.01)
    queue.submit(12, 'A.sln', {'path': 'C:/PLC/Old.TcPOU'})
    assert started.wait(2)
    queue.submit(12, 'A.sln', {'path': 'C:/PLC/New.TcPOU'})
    release.set()
    idle(queue)
    assert len(calls) == 2
    with cache_scope(12, 'A.sln'):
        assert cache.get('Old') is None
        assert cache.get('New') is not None


def test_solution_mismatch_stays_failed_and_is_not_retried_without_guard():
    read = Mock(side_effect=ValueError('solution changed'))
    errors = Mock()
    queue = CacheRefreshQueue(PlcSourceCache(), read, delay=.01, on_error=errors)
    queue.submit(12, 'Old.sln', {'path': 'C:/PLC/MAIN.TcPOU'})
    assert idle(queue)[0]['status'] == 'failed'
    assert read.call_count == 1
    errors.assert_called_once_with(12, 'solution changed')


def test_global_invalidation_during_read_does_not_resurrect_old_values():
    cache = PlcSourceCache()
    def read(*_):
        cache.invalidate()
        return payload(text='pre-write')
    queue = CacheRefreshQueue(cache, read, delay=.01)
    queue.submit(12, 'A.sln', {'path': 'C:/PLC/MAIN.TcPOU'})
    assert idle(queue)[0]['status'] == 'superseded'
    assert cache.snapshot()['count'] == 0


def test_scoped_invalidation_does_not_clear_sibling_member():
    cache = PlcSourceCache()
    cache.put(payload(member='Run'))
    cache.put(payload(member='Stop'))
    cache.invalidate_document(12, 'A.sln', 'C:/PLC/MAIN.TcPOU', 'Run')
    with cache_scope(12, 'A.sln'):
        assert cache.get('MAIN', 'Run') is None
        assert cache.get('MAIN', 'Stop') is not None


def test_refresh_keeps_other_entries_even_when_notification_is_stale():
    cache = PlcSourceCache()
    cache.put(payload('C:/PLC/Keep.TcPOU'))
    live = {'active_document': {'source_file': 'C:/PLC/New.TcPOU', 'member': ''}}
    with patch.object(backend, 'PLC_SOURCE_CACHE', cache), \
         patch.object(backend.ac, 'ps_com', return_value={'solution': 'A.sln'}), \
         patch.object(backend.ac, '_plc_read_current', return_value=live):
        with pytest.raises(StaleCacheNotification):
            backend._refresh_plc_cache(12, 'A.sln', {'path': 'C:/PLC/Old.TcPOU'})
    assert cache.snapshot()['count'] == 1


@pytest.mark.parametrize('pid,solution,path', [(0,'A.sln','C:/MAIN.TcPOU'), (12,'','C:/MAIN.TcPOU'),
                                              (12,'A.sln','C:/private.json')])
def test_invalid_requests_are_rejected(pid, solution, path):
    read = Mock()
    queue = CacheRefreshQueue(PlcSourceCache(), read)
    with pytest.raises(ValueError):
        queue.submit(pid, solution, {'path': path})
    read.assert_not_called()


def test_http_accepts_stale_notice_then_reports_resync():
    cache = PlcSourceCache()
    read = Mock(side_effect=[StaleCacheNotification('moved'), payload('C:/PLC/New.TcPOU')])
    queue = CacheRefreshQueue(cache, read, delay=.01)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), backend._UIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch.object(backend, 'PLC_CACHE_REFRESH', queue), patch.object(backend, 'PLC_SOURCE_CACHE', cache):
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/__plc_cache'
            request = urllib.request.Request(url, data=json.dumps({
                'xae_pid': 12, 'solution': 'A.sln', 'path': 'C:/PLC/Old.TcPOU', 'member': ''
            }).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 200
                assert json.load(response)['status'] == 'queued'
            idle(queue)
            with urllib.request.urlopen(url, timeout=2) as response:
                result = json.load(response)
            assert result['refresh'][0]['status'] == 'resynced'
            assert result['items'][0]['name'] == 'New'
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


@pytest.mark.parametrize('path', ['C:/HMI/Desktop.view', 'C:/HMI/Panel.content', 'C:/HMI/Main.usercontrol'])
def test_hmi_editor_does_not_trigger_plc_read(path):
    context = {'solution': 'A.sln', 'active_document': {'full_name': path}}
    with patch.object(backend.ac, 'ps_com', return_value=context), \
         patch.object(backend.ac, '_plc_read_current') as read:
        with pytest.raises(InactivePlcDocument):
            backend._read_plc_cache_payload(12, 'A.sln', {'path': 'C:/PLC/MAIN.TcPOU'})
        read.assert_not_called()


def test_editor_switch_during_read_is_ignored_but_com_failure_is_not():
    plc = {'solution': 'A.sln', 'active_document': {'full_name': 'C:/PLC/MAIN.TcPOU'}}
    hmi = {'solution': 'A.sln', 'active_document': {'full_name': 'C:/HMI/Desktop.view'}}
    with patch.object(backend.ac, 'ps_com', side_effect=[plc, hmi]), \
         patch.object(backend.ac, '_plc_read_current', side_effect=RuntimeError('editor changed')):
        with pytest.raises(InactivePlcDocument):
            backend._read_plc_cache_payload(12, 'A.sln')
    with patch.object(backend.ac, 'ps_com', return_value=plc), \
         patch.object(backend.ac, '_plc_read_current', side_effect=RuntimeError('COM disconnected')):
        with pytest.raises(RuntimeError, match='COM disconnected'):
            backend._read_plc_cache_payload(12, 'A.sln')


@pytest.mark.parametrize('during_retry', [False, True])
def test_queue_stops_quietly_when_user_leaves_plc_editor(during_retry):
    cache, errors = PlcSourceCache(), Mock()
    cache.put(payload('C:/PLC/Keep.TcPOU'))
    outcomes = ([StaleCacheNotification('switched')] if during_retry else []) + [InactivePlcDocument('HMI')]
    read = Mock(side_effect=outcomes)
    queue = CacheRefreshQueue(cache, read, delay=.01, on_error=errors)
    queue.submit(12, 'A.sln', {'path': 'C:/PLC/MAIN.TcPOU'})
    assert idle(queue)[0]['status'] == 'ignored_non_plc'
    assert read.call_count == len(outcomes)
    errors.assert_not_called()
    assert cache.snapshot()['items'][0]['name'] == 'Keep'
