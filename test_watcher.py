"""Fixture tests for watcher.run state machine; no real Codex calls are made."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher

NOW = 2_000_000_000
THREAD = '00000000-0000-0000-0000-000000000001'


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        home = Path(self.directory.name)
        self.sessions = home / 'sessions'
        self.sessions.mkdir()
        self.patch(watcher, 'STATE_DIR', home / 'state')
        self.patch(watcher, 'STATE_PATH', home / 'state' / 'state.json')
        self.patch(watcher, 'CACHE_PATH', home / 'state' / 'session-cache.json')
        self.patch(watcher, 'LOG_PATH', home / 'state' / 'watcher.log')
        self.patch(watcher, 'LOCK_PATH', home / 'state' / 'monitor.lock')
        self.patch(watcher, 'SESSIONS_DIR', self.sessions)
        self.patch(watcher, 'find_codex', lambda: '/fake/codex')
        self.patch(watcher, 'codex_process_exists', lambda thread: False)
        self.patch(watcher, 'quota_available', lambda exe, candidate, now: True)
        self.dispatches = []
        self.patch(watcher, 'dispatch', self.fake_dispatch)

    def tearDown(self):
        self.directory.cleanup()

    def patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def fake_dispatch(self, executable, thread, message, cwd=None):
        self.dispatches.append((thread, message, cwd))
        behavior = self.dispatch_behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    def write_session(self, records, mtime=NOW - 300, name=None):
        name = name or f'rollout-2026-01-01T00-00-00-{THREAD}.jsonl'
        path = self.sessions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(record) + '\n' for record in records),
                        encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path

    def quota_stall(self, thread=THREAD, turn='turn-1', mtime=NOW - 300):
        stamp = watcher.datetime.fromtimestamp(mtime, tz=watcher.timezone.utc) \
            .isoformat().replace('+00:00', 'Z')
        return [
            {'type': 'session_meta', 'payload': {'cwd': '/tmp/project'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
                'primary': {'used_percent': 100, 'resets_at': NOW - 600},
                'secondary': {'used_percent': 40, 'resets_at': NOW + 9999}}}},
            {'type': 'event_msg', 'payload': {
                'type': 'task_complete', 'turn_id': turn,
                'error': {'codex_error_info': 'usage_limit_exceeded'}}, 'timestamp': stamp},
        ]

    def state(self):
        return json.loads(watcher.STATE_PATH.read_text(encoding='utf-8'))

    def set_state(self, **value):
        watcher.STATE_DIR.mkdir(parents=True, exist_ok=True)
        watcher.STATE_PATH.write_text(json.dumps(value), encoding='utf-8')

    def prime_state(self, since=NOW - 3600):
        self.set_state(version=1, monitoringSince=since, sent={}, activeDispatch=None)
        return since


class RunTests(WatcherCase):
    def test_first_run_primes_monitoring_since(self):
        self.set_state(version=1, monitoringSince=None, sent={}, activeDispatch=None)
        result = watcher.run(NOW, cache={})
        self.assertEqual(result, 'no-quota-stall')
        self.assertEqual(self.state()['monitoringSince'], NOW)

    def test_resumes_newest_quota_stall(self):
        self.prime_state()
        self.write_session(self.quota_stall())
        self.write_session(self.quota_stall(thread='00000000-0000-0000-0000-000000000002',
                                            mtime=NOW - 100),
                           mtime=NOW - 100, name='rollout-x-00000000-0000-0000-0000-000000000002.jsonl')
        self.dispatch_behavior = SimpleNamespace(returncode=0, stderr='')
        result = watcher.run(NOW, cache={})
        self.assertEqual(result, 'resumed')
        self.assertEqual(len(self.dispatches), 1)
        thread, message, cwd = self.dispatches[0]
        self.assertEqual(thread, '00000000-0000-0000-0000-000000000002')
        self.assertEqual(cwd, '/tmp/project')
        self.assertIn(f'{thread}|turn-1', self.state()['sent'])
        self.assertEqual(self.state()['activeDispatch']['threadId'], thread)

    def test_old_stalls_are_ignored(self):
        self.prime_state()
        self.write_session(self.quota_stall(), mtime=NOW - 7200)
        self.assertEqual(watcher.run(NOW, cache={}), 'no-quota-stall')
        self.assertEqual(self.dispatches, [])

    def test_waiting_quota_sends_nothing(self):
        self.prime_state()
        self.write_session(self.quota_stall())
        self.patch(watcher, 'quota_available', lambda exe, candidate, now: False)
        self.assertEqual(watcher.run(NOW, cache={}), 'waiting-quota')
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.state()['sent'], {})

    def test_already_running_guard(self):
        self.prime_state()
        self.write_session(self.quota_stall())
        self.patch(watcher, 'codex_process_exists', lambda thread: True)
        self.assertEqual(watcher.run(NOW, cache={}), 'already-running')
        self.assertEqual(self.dispatches, [])

    def test_dispatch_failure_rolls_back_and_backs_off(self):
        self.prime_state()
        self.write_session(self.quota_stall())
        self.dispatch_behavior = OSError('spawn failed')
        self.assertEqual(watcher.run(NOW, cache={}), 'resume-failed')
        self.assertEqual(self.state()['sent'], {})
        self.assertIsNone(self.state()['activeDispatch'])
        self.assertEqual(len(self.dispatches), 1)
        self.dispatch_behavior = SimpleNamespace(returncode=0, stderr='')
        self.assertEqual(watcher.run(NOW + 10, cache={}), 'retry-backoff')
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(watcher.run(NOW + watcher.RETRY_SECONDS + 1, cache={}), 'resumed')
        self.assertEqual(len(self.dispatches), 2)

    def test_queue_fallback_awaits_start(self):
        self.prime_state()
        self.write_session(self.quota_stall())
        self.dispatch_behavior = SimpleNamespace(returncode=0, stderr='', queued=True)
        self.assertEqual(watcher.run(NOW, cache={}), 'queued-awaiting-start')
        self.assertEqual(watcher.run(NOW + 60, cache={}), 'queued-awaiting-start')
        self.assertEqual(len(self.dispatches), 1)
        result = watcher.run(NOW + watcher.UNCONFIRMED_SECONDS + 1, cache={})
        self.assertEqual(result, 'no-quota-stall')
        state = self.state()
        self.assertIsNone(state['activeDispatch'])
        self.assertIn(f'{THREAD}|turn-1', state['sent'])
        self.assertEqual(len(self.dispatches), 1)

    def test_unstarted_dispatch_is_recovered(self):
        self.prime_state()
        path = self.write_session(self.quota_stall())
        self.dispatch_behavior = SimpleNamespace(returncode=0, stderr='')
        self.assertEqual(watcher.run(NOW, cache={}), 'resumed')
        self.assertEqual(watcher.run(NOW + watcher.UNCONFIRMED_SECONDS + 1, cache={}),
                         'resumed')
        self.assertEqual(len(self.dispatches), 2)
        self.assertIn(f'{THREAD}|turn-1', self.state()['sent'])

    def test_active_dispatch_confirmed_by_new_turn(self):
        self.prime_state()
        path = self.sessions / f'rollout-x-{THREAD}.jsonl'
        records = self.quota_stall()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
        os.utime(path, (NOW - 300, NOW - 300))
        self.dispatch_behavior = SimpleNamespace(returncode=0, stderr='')
        self.assertEqual(watcher.run(NOW, cache={}), 'resumed')
        records.append({'type': 'event_msg', 'payload': {'type': 'task_started',
                                                         'turn_id': 'turn-2'}})
        records.append({'type': 'event_msg', 'payload': {'type': 'task_complete',
                                                         'turn_id': 'turn-2'}})
        path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
        os.utime(path, (NOW - 100, NOW - 100))
        self.assertEqual(watcher.run(NOW + 10, cache={}), 'no-quota-stall')
        self.assertIsNone(self.state()['activeDispatch'])
        self.assertEqual(len(self.dispatches), 1)

    def test_manual_takeover_is_not_resumed(self):
        self.prime_state()
        records = self.quota_stall()
        records.append({'type': 'event_msg', 'payload': {'type': 'task_started',
                                                         'turn_id': 'turn-2'}})
        self.write_session(records)
        self.assertEqual(watcher.run(NOW, cache={}), 'no-quota-stall')
        self.assertEqual(self.dispatches, [])


class UnitTests(WatcherCase):
    def test_thread_id(self):
        self.assertEqual(watcher.thread_id(Path(f'rollout-1-{THREAD}.jsonl')), THREAD)
        self.assertEqual(watcher.thread_id(Path('rollout-nope.jsonl')), None)

    def test_quota_open_from_response(self):
        self.assertIs(watcher.quota_open_from_response({'ordinaryUsageAllowed': True}), True)
        self.assertIs(watcher.quota_open_from_response(
            {'rateLimits': {'primary': {'usedPercent': 100},
                            'secondary': {'usedPercent': 3}}}), False)
        self.assertIs(watcher.quota_open_from_response(
            {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 10},
                                               'secondary': {'usedPercent': 20}}}}), True)
        self.assertIs(watcher.quota_open_from_response({'rateLimits': {}}), None)

    def test_limits_fallback_snake_case(self):
        limits = {'primary': {'resets_at': NOW - 300}, 'secondary': {'resets_at': NOW - 300}}
        self.assertIs(watcher.limits_say_available(limits, NOW), True)
        self.assertIs(watcher.limits_say_available(limits, NOW - 200), False)
        self.assertIs(watcher.limits_say_available({}, NOW), None)


if __name__ == '__main__':
    unittest.main()
