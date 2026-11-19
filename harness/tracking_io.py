"""Bounded inspection I/O and opt-in raw diagnostics; no task status store."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

GIT_TIMEOUT_SECONDS = 5.0
CAPTURE_TIMEOUT_SECONDS = 15.0
LOCK_TIMEOUT_SECONDS = 10.0
_deadline = ContextVar('tracking_deadline', default=None)


def trace(root, event, **fields):
    if os.environ.get('KMP_AGENT_DIAGNOSTICS') != '1': return
    path=Path(root)/'.harness/logs/inspection-diagnostics.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    payload={'event':event,'monotonic':time.monotonic(),'pid':os.getpid(),'thread':threading.get_ident(),**fields}
    fd=os.open(path,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
    try:os.write(fd,(json.dumps(payload,sort_keys=True)+'\n').encode())
    finally:os.close(fd)


@contextmanager
def capture_budget(root, task_id):
    started=time.monotonic();previous=_deadline.get()
    token=_deadline.set(min(previous,started+CAPTURE_TIMEOUT_SECONDS) if previous else started+CAPTURE_TIMEOUT_SECONDS)
    trace(root,'capture_started',task_id=task_id)
    try:
        yield
        check_deadline()
    finally:
        trace(root,'capture_finished',task_id=task_id,elapsed_seconds=time.monotonic()-started)
        _deadline.reset(token)


def check_deadline():
    from harness.command_preflight import current
    budget = current()
    if budget is not None and budget.armed and time.monotonic() >= budget.deadline:
        budget._expired(None, None)
    if _deadline.get() is not None and time.monotonic() >= _deadline.get():
        raise ValueError('capture inspection deadline exceeded; validation not established')


def run_git(root, *args, input=None):
    remaining=GIT_TIMEOUT_SECONDS
    if _deadline.get() is not None:remaining=min(remaining,_deadline.get()-time.monotonic())
    from harness.command_preflight import current
    budget = current()
    preflight_limited = False
    if budget is not None and budget.armed:
        preflight_remaining = budget.deadline-time.monotonic()
        preflight_limited = preflight_remaining <= remaining
        remaining=min(remaining,preflight_remaining)
        if remaining <= 0:budget._expired(None,None)
    if remaining<=0:raise ValueError('capture inspection deadline exceeded; validation not established')
    argv=['rtk','proxy','git',*args];started=time.monotonic()
    trace(root,'git_started',operation=args[0] if args else '',timeout_seconds=remaining)
    child=subprocess.Popen(argv,cwd=root,stdin=subprocess.PIPE if input is not None else None,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    error=None
    try:
        stdout,stderr=child.communicate(input=input,timeout=remaining)
        return subprocess.CompletedProcess(argv,child.returncode,stdout,stderr)
    except BaseException as exc:
        error=type(exc).__name__
        from harness.execution_policy import stop_owned_group
        stop_owned_group(child,lambda owned,timeout:owned.communicate(timeout=timeout),timeout=1)
        if isinstance(exc,subprocess.TimeoutExpired):
            if budget is not None and budget.armed and (preflight_limited or time.monotonic() >= budget.deadline):
                budget._expired(None,None)
            raise ValueError('Git inspection timed out; validation not established') from exc
        raise
    finally:trace(root,'git_finished',operation=args[0] if args else '',pid=child.pid,returncode=child.returncode,error=error,elapsed_seconds=time.monotonic()-started)


def bounded_flock(handle, operation=fcntl.LOCK_EX):
    """Honor a command's deadline without changing non-command lock behavior."""
    from harness.command_preflight import current
    budget = current()
    if budget is None or not budget.armed or operation & fcntl.LOCK_NB:
        return fcntl.flock(handle, operation)
    while True:
        check_deadline()
        try:
            return fcntl.flock(handle, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(.01)


class LockWaitTimeout(TimeoutError):
    def __init__(self, root, path, action, started):
        path = Path(path)
        lock_path = (path.parent.resolve() / path.name).relative_to(Path(root).resolve())
        try:
            owner_path = path.with_name(path.name + '.owner.json')
            fd = os.open(owner_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('lock owner record must be a regular file')
                owner = json.loads(os.read(fd, 4096))
            finally:
                os.close(fd)
            hint = (f"{owner['state']} pid={owner['pid']} action={owner['action']} "
                    f"recorded_at={owner['recorded_at']:.3f} (unverified)")
        except (OSError, ValueError, KeyError, TypeError):
            hint = 'unknown'
        super().__init__(f'{lock_path} timed out after {time.monotonic() - started:.3f}s '
                         f'while waiting for {action}; last owner record: {hint}')


def record_lock_owner(path, action, state):
    from harness.incremental_sync import atomic_json
    try:
        atomic_json(Path(str(path) + '.owner.json'), dict(
            pid=os.getpid(), action=action, state=state, recorded_at=time.time()))
    except OSError:
        pass  # Diagnostics cannot change lock ownership or dispatch results.


@contextmanager
def state_lock(root, path, action):
    start=time.monotonic();trace(root,'state_lock_wait',action=action)
    with Path(path).open('a+') as handle:
        while True:
            check_deadline()
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()-start>=LOCK_TIMEOUT_SECONDS:
                    trace(root,'state_lock_timeout',action=action,wait_seconds=time.monotonic()-start)
                    raise LockWaitTimeout(root, path, action, start)
                time.sleep(.01)
        acquired=time.monotonic();trace(root,'state_lock_acquired',action=action,wait_seconds=acquired-start)
        try:
            record_lock_owner(path, action, 'HELD')
            yield
        finally:
            record_lock_owner(path, action, 'RELEASED')
            fcntl.flock(handle,fcntl.LOCK_UN)
            trace(root,'state_lock_released',action=action,held_seconds=time.monotonic()-acquired)
