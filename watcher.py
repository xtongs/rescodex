#!/usr/bin/env python3
"""Headless watcher that auto-resumes Codex tasks stalled by usage_limit_exceeded.

Each invocation scans ~/.codex/sessions for the newest turn that failed with a
quota error, verifies live quota through `codex app-server`, then resumes the
original task with `codex exec resume` (or `codex queue` when the desktop app
owns the session). Designed to run once a minute via launchd; no GUI, no
notifications, all state stays local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; msvcrt.locking stands in for the lock.
    import msvcrt
    fcntl = None

APP_NAME = 'codex-quota-resume'
STATE_DIR = Path(os.environ.get('QUOTA_RESUME_HOME') or (
    Path.home() / 'AppData/Local' / APP_NAME if os.name == 'nt'
    else Path.home() / 'Library/Application Support' / APP_NAME))
STATE_PATH = STATE_DIR / 'state.json'
CACHE_PATH = STATE_DIR / 'session-cache.json'
LOG_PATH = STATE_DIR / 'watcher.log'
LOCK_PATH = STATE_DIR / 'monitor.lock'
SESSIONS_DIR = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions'
UUID_RE = re.compile(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})', re.I)
RETRY_SECONDS = 300
UNCONFIRMED_SECONDS = 600
RESET_BUFFER_SECONDS = 120
SENT_KEEP = 100
RESUME_MESSAGE = 'Please continue.'


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec='seconds')
    with LOG_PATH.open('a', encoding='utf-8') as stream:
        stream.write(f'{stamp} {message}\n')


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {'version': 1, 'monitoringSince': None, 'sent': {}, 'activeDispatch': None}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state['sent'] = dict(list(state.get('sent', {}).items())[-SENT_KEEP:])
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=STATE_DIR, delete=False) as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    temporary.replace(STATE_PATH)


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for name in list(cache):
        if not Path(name).exists():
            del cache[name]
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=STATE_DIR, delete=False) as stream:
        json.dump(cache, stream, ensure_ascii=False)
        temporary = Path(stream.name)
    temporary.replace(CACHE_PATH)


def find_codex() -> str:
    candidates = [os.environ.get('CODEX_BIN'), shutil.which('codex'),
                  str(Path.home() / '.local/bin/codex'),
                  '/opt/homebrew/bin/codex', '/usr/local/bin/codex']
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return 'codex'


def thread_id(path: Path) -> str | None:
    matches = UUID_RE.findall(path.name)
    return matches[-1] if matches else None


def parse_session(path: Path) -> dict | None:
    """Return the open turn and its quota state, or None when no turn is open.

    A turn stays "open" after task_complete only when it failed with
    usage_limit_exceeded; normal completion and turn_aborted close it.
    """
    open_turn = None
    quota_error = False
    quota_at = None
    latest_limits = None
    cwd = None
    model = None
    approval_policy = None
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get('payload', {})
                if record.get('type') == 'session_meta':
                    cwd = payload.get('cwd')
                    continue
                if record.get('type') == 'turn_context':
                    model = payload.get('model') or model
                    approval_policy = payload.get('approval_policy') or approval_policy
                    continue
                event = payload.get('type')
                if event == 'task_started':
                    open_turn = payload.get('turn_id')
                    quota_error = False
                elif event == 'token_count' and open_turn:
                    new_limits = payload.get('rate_limits') or {}
                    if latest_limits:
                        new_limits = dict(new_limits)
                        for name in ('primary', 'secondary'):
                            if new_limits.get(name) is None:
                                new_limits[name] = latest_limits.get(name)
                    latest_limits = new_limits
                elif event == 'task_complete' and open_turn:
                    if not payload.get('turn_id') or payload.get('turn_id') == open_turn:
                        error = payload.get('error') or {}
                        quota_error = error.get('codex_error_info') == 'usage_limit_exceeded'
                        if quota_error:
                            stamp = record.get('timestamp')
                            if stamp:
                                quota_at = datetime.fromisoformat(
                                    stamp.replace('Z', '+00:00')).timestamp()
                        else:
                            open_turn = None
                            latest_limits = None
                elif event == 'turn_aborted' and open_turn:
                    if not payload.get('turn_id') or payload.get('turn_id') == open_turn:
                        open_turn = None
                        latest_limits = None
    except (OSError, UnicodeError):
        return None
    if not open_turn:
        return None
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return None
    return {'threadId': thread_id(path), 'turnId': open_turn, 'limits': latest_limits,
            'quotaError': quota_error, 'quotaAt': quota_at,
            'path': str(path), 'cwd': cwd, 'modifiedAt': modified_at,
            'model': model, 'approvalPolicy': approval_policy}


def scan_candidates(sessions_dir: Path, since: float, sent: dict, cache: dict) -> dict | None:
    """Newest quota-stalled session modified after `since` whose key was not
    already sent. Files last written before `since` are skipped outright: an
    event can never be newer than the file's last write, so they cannot
    qualify and never need parsing."""
    candidates = []
    for path in sessions_dir.rglob('*.jsonl'):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < since:
            continue
        signature = [stat.st_mtime_ns, stat.st_size]
        cached = cache.get(str(path))
        if cached and cached[0] == signature:
            value = cached[1]
        else:
            value = parse_session(path)
            cache[str(path)] = [signature, value]
        if not value or not value['quotaError'] or not value['threadId']:
            continue
        if (value.get('quotaAt') or value['modifiedAt']) < since:
            continue
        value['key'] = value['threadId'] + '|' + value['turnId']
        if value['key'] in sent:
            continue
        candidates.append(value)
    return max(candidates, key=lambda item: item.get('quotaAt') or item['modifiedAt'],
               default=None)


def quota_open_from_response(response: dict) -> bool | None:
    allowed = response.get('ordinaryUsageAllowed')
    if isinstance(allowed, bool):
        return allowed
    limits = (response.get('rateLimitsByLimitId') or {}).get('codex') \
        or response.get('rateLimits') or {}
    windows = [limits.get('primary'), limits.get('secondary')]
    if not all(window and window.get('usedPercent') is not None for window in windows):
        return None
    return not limits.get('spendControlReached') \
        and all(window['usedPercent'] < 100 for window in windows)


def limits_say_available(limits: dict | None, now: float) -> bool | None:
    if not limits:
        return None
    resets = [window.get('resets_at') for window in
              (limits.get('primary'), limits.get('secondary')) if window and window.get('resets_at')]
    if not resets:
        return None
    return all(now >= reset + RESET_BUFFER_SECONDS for reset in resets)


class AppServerConnection:
    """Minimal JSON-RPC client over `codex app-server --stdio`."""

    def __init__(self, executable: str):
        import queue
        import threading
        self.process = subprocess.Popen(
            [executable, 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
        self.replies = queue.Queue()

        def read():
            try:
                for line in self.process.stdout:
                    self.replies.put(json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                self.replies.put(None)

        threading.Thread(target=read, daemon=True).start()
        self.next_id = 0
        self.request('initialize', {'clientInfo': {'name': 'rescodex-watcher', 'version': '1.0'},
                                    'capabilities': {'experimentalApi': True}})
        assert self.process.stdin
        self.process.stdin.write('{"method":"initialized"}\n')
        self.process.stdin.flush()

    def request(self, method: str, params: dict):
        self.next_id += 1
        assert self.process.stdin
        self.process.stdin.write(json.dumps(
            {'id': self.next_id, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 25
        while True:
            try:
                reply = self.replies.get(timeout=max(0, deadline - time.monotonic()))
            except Exception:
                raise TimeoutError(f'{method}: app-server timeout')
            if reply is None:
                raise OSError('app-server connection closed')
            if reply.get('id') == self.next_id:
                if 'error' in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message', 'request failed')}")
                return reply['result']

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()


def live_quota_available(executable: str) -> bool | None:
    try:
        connection = AppServerConnection(executable)
    except (OSError, RuntimeError, TimeoutError) as error:
        log(f'app-server unavailable: {type(error).__name__}: {error}')
        return None
    try:
        return quota_open_from_response(connection.request('account/rateLimits/read', {}))
    except (OSError, RuntimeError, TimeoutError) as error:
        log(f'rateLimits/read failed: {type(error).__name__}: {error}')
        return None
    finally:
        connection.close()


def quota_available(executable: str, candidate: dict, now: float) -> bool | None:
    live = live_quota_available(executable)
    if live is not None:
        return live
    return limits_say_available(candidate.get('limits'), now)


def codex_process_exists(thread: str) -> bool | None:
    if os.name == 'nt':
        # No pgrep on Windows: ask PowerShell whether any command line mentions the thread.
        query = f"(Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%{thread}%'\").Count -gt 0"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', query],
                                capture_output=True, text=True)
        return None if result.returncode else result.stdout.strip() == 'True'
    result = subprocess.run(['pgrep', '-f', thread], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def dispatch(executable: str, thread: str, message: str, cwd: str | None = None,
             model: str | None = None, approval_policy: str | None = None):
    command = [executable, 'exec', 'resume', '--skip-git-repo-check', '--json']
    # Replay the interrupted turn's recorded model/approval so the resumed turn
    # keeps the session's settings instead of whatever config.toml has now.
    if model:
        command += ['-c', f'model={json.dumps(model)}']
    if approval_policy:
        command += ['-c', f'approval_policy={json.dumps(approval_policy)}']
    command += [thread, message]
    result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')
    if result.returncode and 'active writer' in result.stderr:
        # The desktop app owns this session: hand the message to its writer.
        # Config overrides are omitted: the desktop process uses its own runtime.
        return queue_dispatch(executable, thread, message)
    return result


def queue_dispatch(executable: str, thread: str, message: str):
    command = [executable, 'queue', '--thread', thread, '--message', message]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    result.queued = result.returncode == 0
    return result


def rollback(state: dict, candidate: dict) -> None:
    state.get('sent', {}).pop(candidate['key'], None)
    state['activeDispatch'] = None
    save_state(state)


def run(now: float, dry_run: bool = False, cache: dict | None = None) -> str:
    state = load_state()
    cache = cache if cache is not None else {}
    since = state.get('monitoringSince')
    if since is None:
        # First run ever: only interruptions happening from now on are resumed.
        state['monitoringSince'] = since = now
        save_state(state)

    active = state.get('activeDispatch')
    if active:
        current = parse_session(Path(active['path']))
        if not (current and current['turnId'] == active['turnId'] and current['quotaError']):
            state['activeDispatch'] = None
            save_state(state)
            log(f'dispatch confirmed thread={active["threadId"]} (session moved on)')
        elif active.get('deliveryMode') == 'queued':
            elapsed = now - state.get('sent', {}).get(active['key'], now)
            if elapsed < UNCONFIRMED_SECONDS:
                return 'queued-awaiting-start'
            state['activeDispatch'] = None
            save_state(state)
            log(f'queue never started after {int(elapsed)}s; giving up thread={active["threadId"]}')
        else:
            elapsed = now - state.get('sent', {}).get(active['key'], now)
            if codex_process_exists(active['threadId']):
                return 'dispatch-running'
            if elapsed < UNCONFIRMED_SECONDS:
                return 'waiting-start'
            state['sent'].pop(active['key'], None)
            state['activeDispatch'] = None
            save_state(state)
            log(f'dispatch never started after {int(elapsed)}s; recovering thread={active["threadId"]}')

    candidate = scan_candidates(SESSIONS_DIR, since, state.get('sent', {}), cache)
    if not candidate:
        return 'no-quota-stall'
    if dry_run:
        return f'dry-run-due thread={candidate["threadId"]} turn={candidate["turnId"]}'
    if codex_process_exists(candidate['threadId']):
        return 'already-running'
    available = quota_available(find_codex(), candidate, now)
    if available is not True:
        return 'waiting-quota' if available is False else 'quota-unknown'

    last_attempt = state.get('lastAttempt') or {}
    if last_attempt.get('key') == candidate['key'] and now - last_attempt.get('at', 0) < RETRY_SECONDS:
        return 'retry-backoff'
    state['lastAttempt'] = {'key': candidate['key'], 'at': now}
    # Persist before dispatch: a crash between here and process start must not
    # send twice, so the key is recorded first and rolled back on failure.
    state['sent'][candidate['key']] = now
    state['activeDispatch'] = candidate
    save_state(state)

    try:
        result = dispatch(find_codex(), candidate['threadId'], RESUME_MESSAGE, candidate.get('cwd'),
                          model=candidate.get('model'),
                          approval_policy=candidate.get('approvalPolicy'))
    except OSError as error:
        rollback(state, candidate)
        log(f'could not start resume thread={candidate["threadId"]}: {error}')
        return 'resume-failed'
    if result.returncode:
        rollback(state, candidate)
        log(f'resume failed thread={candidate["threadId"]} code={result.returncode} '
            f'{result.stderr[-300:] if result.stderr else ""}')
        return 'resume-failed'
    if getattr(result, 'queued', False):
        state['activeDispatch']['deliveryMode'] = 'queued'
        save_state(state)
        log(f'queued thread={candidate["threadId"]}; awaiting observed start')
        return 'queued-awaiting-start'
    log(f'resumed thread={candidate["threadId"]} turn={candidate["turnId"]}')
    return 'resumed'


def try_lock(stream) -> bool:
    try:
        if fcntl:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def run_once(dry_run: bool = False) -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a+b') as lock:
        if not try_lock(lock):
            return 'monitor-busy'
        cache = load_cache()
        result = run(time.time(), dry_run=dry_run, cache=cache)
        state = load_state()
        now = time.time()
        if state.get('status') != result:
            log(result)
            state['lastLoggedAt'] = now
        elif result != 'no-quota-stall' and now - state.get('lastLoggedAt', 0) >= 600:
            # Keep long waits traceable without spamming one line per minute.
            log(f'still {result}')
            state['lastLoggedAt'] = now
        state.update(status=result, lastCheckedAt=now)
        save_state(state)
        save_cache(cache)
        return result


def show_status() -> None:
    state = load_state()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if not SESSIONS_DIR.is_dir():
        print(f'sessions directory missing: {SESSIONS_DIR}')
        return
    candidate = scan_candidates(SESSIONS_DIR, state.get('monitoringSince') or 0,
                                state.get('sent', {}), {})
    if candidate:
        quota_at = candidate.get('quotaAt')
        readable = datetime.fromtimestamp(quota_at).astimezone().isoformat() if quota_at else '?'
        print(f"candidate: thread={candidate['threadId']} turn={candidate['turnId']} "
              f"quotaAt={readable} cwd={candidate.get('cwd')}")
    else:
        print('candidate: none')


def parse_session_on(write, path, records):
    write(records)
    return parse_session(path)


def self_test() -> None:
    import tempfile
    now = 2_000_000_000
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'rollout-2026-01-01T00-00-00-00000000-0000-0000-0000-000000000001.jsonl'

        def write(records):
            path.write_text(''.join(json.dumps(record) + '\n' for record in records),
                            encoding='utf-8')
            os.utime(path, (now - 300, now - 300))

        meta = {'type': 'session_meta', 'payload': {'cwd': '/tmp/project', 'session_id': 'x'}}
        context = {'type': 'turn_context', 'payload': {'approval_policy': 'on-request',
                                                       'model': 'gpt-fixture'}}
        turn1 = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1'}}
        limits = {'rate_limits': {'primary': {'used_percent': 100, 'resets_at': now - 600},
                                  'secondary': {'used_percent': 40, 'resets_at': now + 9999}}}
        count = {'type': 'event_msg', 'payload': {'type': 'token_count', **limits}}
        stamp = datetime.fromtimestamp(now - 300, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        quota_fail = lambda turn: {'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn,
            'error': {'message': 'limit', 'codex_error_info': 'usage_limit_exceeded'}},
            'timestamp': stamp}

        running = parse_session_on(write, path, [meta, turn1])
        assert running and running['quotaError'] is False and running['cwd'] == '/tmp/project'

        done = parse_session_on(write, path, [meta, turn1, count,
                                              {'type': 'event_msg', 'payload': {
                                                  'type': 'task_complete', 'turn_id': 'turn-1'}}])
        assert done is None

        stalled = parse_session_on(write, path, [meta, context, turn1, count, quota_fail('turn-1')])
        assert stalled and stalled['quotaError'] and stalled['turnId'] == 'turn-1'
        assert stalled['limits']['primary']['used_percent'] == 100
        assert stalled['model'] == 'gpt-fixture' and stalled['approvalPolicy'] == 'on-request'

        aborted = parse_session_on(write, path, [meta, turn1, count, quota_fail('turn-1'),
                                                 {'type': 'event_msg', 'payload': {
                                                     'type': 'turn_aborted', 'turn_id': 'turn-1'}}])
        assert aborted is None

        manual = parse_session_on(write, path, [meta, turn1, count, quota_fail('turn-1'),
                                                {'type': 'event_msg', 'payload': {
                                                    'type': 'task_started', 'turn_id': 'turn-2'}}])
        assert manual and manual['quotaError'] is False and manual['turnId'] == 'turn-2'

        turn2 = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-2'}}
        restalled = parse_session_on(write, path, [meta, turn1, count, quota_fail('turn-1'),
                                                   turn2, quota_fail('turn-2')])
        assert restalled and restalled['turnId'] == 'turn-2'

        cache = {}
        assert scan_candidates(Path(directory), now - 3600, {}, cache) is not None
        assert scan_candidates(Path(directory), now + 1, {}, cache) is None
        found = scan_candidates(Path(directory), now - 3600, {}, cache)
        assert found['key'] == '00000000-0000-0000-0000-000000000001|turn-2'
        assert scan_candidates(Path(directory), now - 3600, {found['key']: 1}, cache) is None

    assert quota_open_from_response({'ordinaryUsageAllowed': True}) is True
    assert quota_open_from_response({'ordinaryUsageAllowed': False}) is False
    assert quota_open_from_response({'rateLimits': {
        'primary': {'usedPercent': 99}, 'secondary': {'usedPercent': 100}}}) is False
    assert quota_open_from_response({'rateLimits': {}}) is None
    stalled_limits = {'primary': {'resets_at': now - 300}, 'secondary': {'resets_at': now - 300}}
    assert limits_say_available(stalled_limits, now - 100) is True
    assert limits_say_available(stalled_limits, now - 350) is False
    assert limits_say_available(None, now) is None
    assert limits_say_available({'primary': {}}, now) is None
    print('SELF_TEST_OK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report the candidate, send nothing')
    parser.add_argument('--status', action='store_true', help='print state and current candidate')
    parser.add_argument('--self-test', action='store_true', help='run built-in fixtures')
    args = parser.parse_args()
    if args.status:
        show_status()
    elif args.self_test:
        self_test()
    elif args.dry_run:
        print(run_once(dry_run=True))
    else:
        try:
            run_once()
        except Exception as error:
            log(f'error {type(error).__name__}: {error}')
            raise
