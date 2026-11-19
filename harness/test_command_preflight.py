"""Slow synthetic checks and real disposable children; no product processes."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from harness.command_preflight import Preflight, PreflightTimeout
from harness.execution_policy import managed_command
from harness import test_execution_policy as fixtures


class CommandPreflightTests(unittest.TestCase):
    _fixture_setup = fixtures.ExecutionPolicyTests.setUp
    step = fixtures.ExecutionPolicyTests.step
    write_native = fixtures.ExecutionPolicyTests.write_native
    activate = fixtures.ExecutionPolicyTests.activate
    plan = fixtures.ExecutionPolicyTests.plan

    def setUp(self):
        self._fixture_setup()
        self.activate(); self.plan(); self.m.ready('T')
        self.work = self.m.begin('T','improvement')

    def command(self, **kwargs):
        return managed_command(self.m,'T',[sys.executable,'-c','print("synthetic check")'],self.root,**kwargs)

    def diagnostics(self):
        return [json.loads(line) for path in (self.root/'.harness/logs/command-preflight').glob('*.jsonl')
                for line in path.read_text().splitlines()]

    def test_blocked_initial_dispatch_times_out_before_child_or_owner_mutation(self):
        original = self.m.read()['tasks']['T']['executions'][0]
        start = time.monotonic()
        with patch.object(self.m,'require_dispatch',side_effect=lambda *a,**k:time.sleep(2)), \
                patch('harness.execution_policy.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(PreflightTimeout,'ownership_dispatch'):
                self.command(preflight_timeout=.1)
            spawn.assert_not_called()
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(self.m.read()['tasks']['T']['executions'][0],original)
        self.assertEqual(self.diagnostics()[-1]['state'],'TIMED_OUT')

    def test_clean_prelaunch_timeout_allows_retry_on_same_attempt_without_fake_exit(self):
        with patch('harness.execution_policy.WorkerGate',side_effect=lambda *a,**kw:time.sleep(2)), \
                patch('harness.execution_policy.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(PreflightTimeout,'worker_gate'):
                self.command(preflight_timeout=.5)
            spawn.assert_not_called()
        task = self.m.read()['tasks']['T']; run = task['executions'][0]
        self.assertEqual(run['attempt_id'],self.work['attempt_id'])
        self.assertEqual(len(run['command_prelaunch_aborts']),1)
        self.assertFalse(run.get('command_owner_completions'))
        self.assertEqual(self.command().returncode,0)
        self.assertEqual(len(self.m.read()['tasks']['T']['executions']),1)

    def test_timeout_cleanup_never_replays_the_blocked_validator(self):
        from harness import development_tracking
        original = development_tracking.refresh
        blocked = [False]
        def refresh(*args,**kwargs):
            if blocked[0]:time.sleep(2)
            return original(*args,**kwargs)
        def stalled_gate(*args):
            blocked[0] = True
            time.sleep(2)
        before = self.native.read_bytes()
        start = time.monotonic()
        with patch('harness.development_tracking.refresh',side_effect=refresh), \
                patch('harness.execution_policy.WorkerGate',side_effect=stalled_gate):
            with self.assertRaisesRegex(PreflightTimeout,'worker_gate'):
                self.command(preflight_timeout=.2)
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(self.native.read_bytes(),before)
        run=self.m.read()['tasks']['T']['executions'][0]
        self.assertEqual(run['attempt_id'],self.work['attempt_id'])
        self.assertEqual(len(run['command_prelaunch_aborts']),1)

    def test_pending_spawn_timeout_remains_uncertain_and_needs_existing_recovery(self):
        from harness.recovery import spawn_intent
        def stalled(*args):
            value = spawn_intent(*args)
            time.sleep(2)
            return value
        with patch('harness.recovery.spawn_intent',side_effect=stalled), \
                patch('harness.execution_policy.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(PreflightTimeout,'spawn_intent'):
                self.command(preflight_timeout=.5)
            spawn.assert_not_called()
        run = self.m.read()['tasks']['T']['executions'][0]
        self.assertFalse(run.get('command_prelaunch_aborts'))
        with self.assertRaisesRegex((ValueError,RuntimeError),'explicit recovery'):
            self.command()

    def test_process_creation_is_separate_and_command_runtime_not_limited_by_preflight(self):
        observed = []
        result = managed_command(self.m,'T',[sys.executable,'-c','import time;time.sleep(.3)'],
                                 self.root,preflight_timeout=.2,on_spawn=lambda child:observed.append(child.pid))
        self.assertEqual(result.returncode,0)
        self.assertEqual(len(observed),1)
        self.assertTrue(any(r['state']=='SPAWNED' and r['pid']==observed[0] for r in self.diagnostics()))

    def test_spawn_is_observed_before_a_delayed_registration(self):
        from harness.recovery import register_process
        from harness.command_preflight import current
        observed = []
        def delayed(*args):
            child=args[2]
            self.assertTrue(current().spawned)
            self.assertEqual(observed,[child.pid])
            self.assertTrue(any(row['state']=='SPAWNED' and row.get('pid')==child.pid
                                for row in self.diagnostics()))
            self.assertTrue(any(row['stage']=='process_registration' and row['state']=='STARTED'
                                for row in self.diagnostics()))
            time.sleep(.1)
            return register_process(*args)
        with patch('harness.recovery.register_process',side_effect=delayed):
            result=self.command(on_spawn=lambda child:observed.append(child.pid))
        self.assertEqual(result.returncode,0)

    def test_main_thread_timeout_restores_alarm_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(PreflightTimeout):
            with Preflight(self.root,.02):time.sleep(.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM),previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertEqual(self.diagnostics()[-1]['deadline_mode'],'SIGNAL')

    def test_worker_thread_runs_managed_command_without_changing_parent_alarm(self):
        previous=signal.getsignal(signal.SIGALRM)
        handler=lambda *args:None
        signal.signal(signal.SIGALRM,handler)
        signal.setitimer(signal.ITIMER_REAL,5)
        results=[]; errors=[]
        try:
            def invoke():
                try:results.append(self.command())
                except BaseException as error:errors.append(error)
            with patch('harness.command_preflight.signal.signal',side_effect=AssertionError('worker changed handler')), \
                    patch('harness.command_preflight.signal.setitimer',side_effect=AssertionError('worker changed timer')):
                worker=threading.Thread(target=invoke);worker.start();worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(results[0].returncode,0)
            self.assertIs(signal.getsignal(signal.SIGALRM),handler)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0],0)
            self.assertTrue(all(row['deadline_mode']=='COOPERATIVE' for row in self.diagnostics()))
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous)

    def test_worker_thread_deadline_rejects_slow_stage_before_spawning(self):
        errors=[]
        def invoke():
            try:self.command(preflight_timeout=.03)
            except BaseException as error:errors.append(error)
        start=time.monotonic()
        with patch.object(self.m,'require_dispatch',side_effect=lambda *args,**kwargs:time.sleep(.1)), \
                patch('harness.execution_policy.subprocess.Popen') as spawn:
            worker=threading.Thread(target=invoke);worker.start();worker.join(2)
            spawn.assert_not_called()
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0],PreflightTimeout)
        self.assertIn('ownership_dispatch',str(errors[0]))
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(self.diagnostics()[-1]['state'],'TIMED_OUT')
        self.assertEqual(self.diagnostics()[-1]['deadline_mode'],'COOPERATIVE')

    def test_worker_rechecks_deadline_after_launch_diagnostic(self):
        emit = Preflight.emit
        errors = []

        def expire_after_launch_diagnostic(budget, state, **extra):
            emit(budget, state, **extra)
            if budget.name == 'process_launch' and state == 'STARTED':
                budget.deadline = budget.started

        def invoke():
            try:
                self.command()
            except Exception as error:
                errors.append(error)

        with patch.object(Preflight, 'emit', expire_after_launch_diagnostic), \
                patch('harness.execution_policy.subprocess.Popen') as spawn:
            worker = threading.Thread(target=invoke)
            worker.start()
            worker.join(2)
            spawn.assert_not_called()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PreflightTimeout)
        self.assertIn('process_launch', str(errors[0]))

    def test_invalid_deadlines_fail_before_any_spawn(self):
        for value in (0,-1,float('inf'),float('nan'),True,'60'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.command(preflight_timeout=value)


class LoggedPreflightTests(unittest.TestCase):
    def test_request_process_spawn_and_exit_events_are_distinct_and_correlated(self):
        from harness.logging import Logger
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);logger=Logger(root);rid=logger.start('HARNESS_BUILD','improvement','Synthetic')
            self.assertEqual(logger.command(rid,['rtk','proxy',sys.executable,'-c','print("synthetic")']),0)
            events=[e for e in logger.read() if e['event_type'].startswith('command_')]
            self.assertEqual([e['event_type'] for e in events],
                             ['command_started','command_process_spawned','command_finished'])
            self.assertEqual(len({e['observed']['call_id'] for e in events}),1)
            self.assertGreater(events[1]['observed']['pid'],0)
            self.assertEqual(events[0]['observed']['launch_status'],'REQUESTED')
            self.assertEqual(events[-1]['observed']['returncode'],0)

    def test_snapshot_timeout_is_bounded_and_never_launches_child(self):
        from harness.logging import Logger
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);logger=Logger(root);rid=logger.start('HARNESS_BUILD','improvement','Synthetic')
            with patch('harness.logging.snapshot',side_effect=lambda *a,**kw:time.sleep(2)) as snapshots, \
                    patch('harness.logging.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(PreflightTimeout,'snapshot_before'):
                    logger.command(rid,['rtk','proxy',sys.executable,'-c','print("fixture")'],preflight_timeout=.1)
                self.assertEqual(snapshots.call_count,1);spawn.assert_not_called()
            self.assertFalse(any(e['event_type']=='command_process_spawned' for e in logger.read()))


if __name__ == '__main__':unittest.main()
