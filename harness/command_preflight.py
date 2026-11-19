"""Bounded command checks and diagnostic progress; never an approval cache.

Main-thread commands use an alarm; existing worker threads use the same total
deadline cooperatively before and after stages and through bounded inspection
I/O. Worker threads never change a process-wide signal handler or timer. A
blocking kernel read cannot be preempted cooperatively. Process creation is an
explicit boundary: interrupting Popen before it returns can lose the only safe
child handle, so prelaunch checks stop immediately before that boundary.
"""
import contextlib
import contextvars
import functools
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

DEFAULT_TIMEOUT = 60.0
_current = contextvars.ContextVar('command_preflight', default=None)


class PreflightTimeout(RuntimeError):
    pass


class Preflight:
    def __init__(self, root, timeout=DEFAULT_TIMEOUT, call_id=None):
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('command preflight timeout must be positive finite seconds')
        self.root = Path(root).resolve()
        self.timeout = float(timeout)
        self.call_id = call_id or str(uuid.uuid4())
        if not isinstance(self.call_id,str) or str(uuid.UUID(self.call_id)) != self.call_id:
            raise ValueError('canonical command preflight call ID required')
        self.started = time.monotonic()
        self.deadline = self.started + self.timeout
        self.name = 'initialization'
        self.armed = False
        self.spawned = False
        self.records = []
        self.previous_timer = self.previous_handler = None
        self.mode = 'COOPERATIVE'

    def emit(self, state, **extra):
        record = dict(call_id=self.call_id, stage=self.name, state=state,
                      elapsed_seconds=time.monotonic()-self.started,
                      timeout_seconds=self.timeout, deadline_mode=self.mode, **extra)
        self.records.append(record)
        line = (json.dumps(record, sort_keys=True)+'\n').encode()
        # Diagnostics contain no argv, source bytes or approval contents.
        folder = self.root/'.harness/logs/command-preflight'
        if any(p.is_symlink() for p in (self.root/'.harness', self.root/'.harness/logs', folder)):
            raise ValueError('command preflight diagnostic directory cannot be symlink')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(self.call_id+'.jsonl')
        if path.is_symlink():
            raise ValueError('command preflight diagnostic file cannot be symlink')
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if not written:
                    raise OSError('command preflight diagnostic append made no progress')
                view = view[written:]
        finally:
            os.close(descriptor)
        os.write(2, b'kmp-agent preflight '+line)

    def _expired(self, signum, frame):
        raise PreflightTimeout('command preflight timed out after %.3fs at %s; '
            'no test success is implied; inspect .harness/logs/command-preflight/%s.jsonl '
            'and task ownership before retry' % (self.timeout, self.name, self.call_id))

    def __enter__(self):
        self.mode = ('SIGNAL' if threading.current_thread() is threading.main_thread()
                     and hasattr(signal, 'setitimer') else 'COOPERATIVE')
        if self.mode == 'SIGNAL':
            self.previous_handler = signal.getsignal(signal.SIGALRM)
            self.previous_timer = signal.getitimer(signal.ITIMER_REAL)
            # Do not steal another subsystem's pending alarm.
            if self.previous_timer[0] > 0:
                raise ValueError('bounded command preflight cannot replace an active process alarm')
        from harness.decisions import native_record_batch
        self.batch = native_record_batch()
        self.batch.__enter__()
        self.token = _current.set(self)
        try:
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                self._expired(None,None)
            self.armed = True
            if self.mode == 'SIGNAL':
                signal.signal(signal.SIGALRM, self._expired)
                signal.setitimer(signal.ITIMER_REAL, remaining)
            return self
        except BaseException:
            self.stop_checks()
            if self.mode == 'SIGNAL':
                signal.signal(signal.SIGALRM,self.previous_handler)
                signal.setitimer(signal.ITIMER_REAL,*self.previous_timer)
            _current.reset(self.token)
            self.batch.__exit__(None,None,None)
            raise

    def stop_checks(self):
        if self.armed:
            if self.mode == 'SIGNAL':
                signal.setitimer(signal.ITIMER_REAL, 0)
            self.armed = False

    def __exit__(self, kind, error, traceback):
        self.stop_checks()
        if self.mode == 'SIGNAL':
            signal.signal(signal.SIGALRM, self.previous_handler)
            signal.setitimer(signal.ITIMER_REAL, *self.previous_timer)
        _current.reset(self.token)
        self.batch.__exit__(kind,error,traceback)
        if kind and issubclass(kind, PreflightTimeout):
            self.emit('TIMED_OUT', error=str(error), process_spawned=self.spawned)
        elif kind:
            self.emit('FAILED', error_type=kind.__name__, process_spawned=self.spawned)

    @contextlib.contextmanager
    def stage(self, name):
        self.name = name
        if self.armed and time.monotonic() >= self.deadline:
            self._expired(None, None)
        self.emit('STARTED')
        start = time.monotonic()
        try:
            yield
        except BaseException:
            raise
        else:
            if self.armed and time.monotonic() >= self.deadline:
                self._expired(None,None)
            self.emit('FINISHED', stage_elapsed_seconds=time.monotonic()-start)


def current():
    return _current.get()


@contextlib.contextmanager
def stage(name):
    budget = current()
    if budget is None:
        yield
    else:
        with budget.stage(name):
            yield


def bounded(function):
    """Include ownership decorators in the same budget as Logger.command."""
    @functools.wraps(function)
    def wrapped(owner, *args, **kwargs):
        timeout = kwargs.get('preflight_timeout', DEFAULT_TIMEOUT)
        if current() is not None:
            return function(owner, *args, **kwargs)
        with Preflight(owner.root, timeout):
            return function(owner, *args, **kwargs)
    return wrapped
