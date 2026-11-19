"""Publication failure, recovery and reader isolation, independent of approval QA."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from harness import contract_transition_transaction as transaction
from harness import integration_transaction as integration


class ContractTransitionTransaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        directory = self.root / '.harness/tasks'
        directory.mkdir(parents=True)
        self.canonical = self.root / 'requirement-lineage.json'
        self.canonical.write_bytes(b'{"policy":"old"}\n')
        self.path = directory / 'tasks.yaml'
        self.original = {'tasks': {'T': {'task_id': 'T', 'version': 'old'}}, 'outbox': []}
        self.path.write_text(json.dumps(self.original))
        self.before = (self.canonical.read_bytes(), self.path.read_bytes())
        self.flushed = []

        def save(state):
            integration._write(self.path, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode())

        def flush(state):
            self.flushed.extend(state['outbox'])
            if state['outbox']:
                state['outbox'] = []
                save(state)

        self.manager = types.SimpleNamespace(root=self.root, directory=directory,
                                             path=self.path, _save=save, _flush=flush)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Core transition authority has separate integration tests. These tests
        # isolate publication, including failures after approval preparation.
        for name in ('harness.protected_verification.validate_ledger',
                     'harness.development_tracking.refresh',
                     'harness.verification.refresh', 'harness.decisions.refresh_waits',
                     'harness.execution_policy.gate', 'harness.recovery.progress_gate'):
            self.stack.enter_context(patch(name))

    def prepare(self, state):
        state['tasks']['T']['version'] = 'new'
        state['outbox'].append({'event': 'transition'})
        return {'status': 'APPLIED'}, 'requirement-lineage.json', b'{"policy":"new"}\n'

    def crash(self, boundary):
        def fault(phase, path):
            if (phase, path) == boundary:
                raise RuntimeError('simulated process interruption')
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            transaction.run(self.manager, 'T', self.prepare, fault_hook=fault)
        pending = transaction.unresolved(self.root)
        self.assertEqual(len(pending), 1)
        with self.assertRaisesRegex(ValueError, 'explicit recovery'):
            with integration.read_barrier(self.root):
                self.fail('partially published contract was observable')
        self.assertEqual(self.flushed, [])
        return pending[0]['transaction_id']

    def test_commits_both_files_before_flushing_events(self):
        original_flush = self.manager._flush

        def flush(state):
            if state['outbox']:
                self.assertEqual(transaction.unresolved(self.root), [])
                self.assertEqual(json.loads(self.canonical.read_text())['policy'], 'new')
                self.assertEqual(json.loads(self.path.read_text())['tasks']['T']['version'], 'new')
            original_flush(state)

        self.manager._flush = flush
        self.assertEqual(transaction.run(self.manager, 'T', self.prepare), {'status': 'APPLIED'})
        self.assertEqual(self.flushed, [{'event': 'transition'}])
        with integration.read_barrier(self.root):
            self.assertEqual(json.loads(self.path.read_text())['tasks']['T']['version'], 'new')

    def test_each_publication_boundary_recovers_exact_before_bytes(self):
        for boundary in (('prepared', None), ('applied', 'requirement-lineage.json'),
                         ('applied', '.harness/tasks/tasks.yaml')):
            with self.subTest(boundary=boundary):
                identity = self.crash(boundary)
                result = transaction.recover(self.manager, identity, 'retry approved transition')
                self.assertEqual(result['state'], 'ROLLED_BACK')
                self.assertEqual((self.canonical.read_bytes(), self.path.read_bytes()), self.before)
                self.assertEqual(transaction.unresolved(self.root), [])
                self.assertEqual(transaction.recover(self.manager, identity, 'repeat'), result)

    def test_unknown_edits_reject_recovery_without_overwriting_either_file(self):
        identity = self.crash(('applied', '.harness/tasks/tasks.yaml'))
        self.canonical.write_bytes(b'unknown external edit')
        current = (self.canonical.read_bytes(), self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'unknown edits'):
            transaction.recover(self.manager, identity, 'restore')
        self.assertEqual((self.canonical.read_bytes(), self.path.read_bytes()), current)
        self.assertEqual(len(transaction.unresolved(self.root)), 1)

    def test_corrupt_snapshot_rejects_before_restoring_any_file(self):
        identity = self.crash(('applied', '.harness/tasks/tasks.yaml'))
        folder = transaction._base(self.root) / identity
        (folder / '0-before.bin').chmod(0o600)
        (folder / '0-before.bin').write_bytes(b'forged')
        current = (self.canonical.read_bytes(), self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            transaction.recover(self.manager, identity, 'restore')
        self.assertEqual((self.canonical.read_bytes(), self.path.read_bytes()), current)

    def test_staging_interruption_recovers_without_touching_targets(self):
        original_write = integration._write

        def write(path, data, mode=0o600):
            if Path(path).name == '0-after.bin':
                raise RuntimeError('interrupted staging')
            return original_write(path, data, mode)

        with patch.object(integration, '_write', side_effect=write):
            with self.assertRaisesRegex(RuntimeError, 'staging'):
                transaction.run(self.manager, 'T', self.prepare)
        identity = transaction.unresolved(self.root)[0]['transaction_id']
        transaction.recover(self.manager, identity, 'retry staging')
        self.assertEqual((self.canonical.read_bytes(), self.path.read_bytes()), self.before)
        self.assertEqual(transaction.unresolved(self.root), [])

    def test_flush_failure_keeps_committed_contract_and_durable_event(self):
        original_flush = self.manager._flush

        def flush(state):
            if state['outbox']:
                raise RuntimeError('logger unavailable')
            original_flush(state)

        self.manager._flush = flush
        with self.assertRaisesRegex(RuntimeError, 'logger unavailable'):
            transaction.run(self.manager, 'T', self.prepare)
        self.assertEqual(transaction.unresolved(self.root), [])
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['tasks']['T']['version'], 'new')
        self.assertEqual(stored['outbox'], [{'event': 'transition'}])
        original_flush(stored)
        self.assertEqual(self.flushed, [{'event': 'transition'}])

    def test_concurrent_reader_waits_for_settled_pair(self):
        writing = threading.Event()
        release = threading.Event()
        observed = []
        errors = []

        def fault(phase, path):
            if (phase, path) == ('applied', 'requirement-lineage.json'):
                writing.set()
                if not release.wait(3):
                    raise RuntimeError('test writer timed out')

        def writer():
            try:
                transaction.run(self.manager, 'T', self.prepare, fault_hook=fault)
            except BaseException as exc:
                errors.append(exc)

        def reader():
            try:
                with integration.read_barrier(self.root):
                    observed.append((json.loads(self.canonical.read_text())['policy'],
                                     json.loads(self.path.read_text())['tasks']['T']['version']))
            except BaseException as exc:
                errors.append(exc)

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)
        writer_thread.start()
        self.assertTrue(writing.wait(3))
        reader_thread.start()
        reader_thread.join(.05)
        self.assertTrue(reader_thread.is_alive())
        release.set()
        writer_thread.join(3)
        reader_thread.join(3)
        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(reader_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [('new', 'new')])

    def test_guard_allows_bound_recovery_with_unresolved_journal(self):
        from harness.product_guard import evaluate
        self.original['execution_policy_bindings'] = {'R': {'root_session_id': 'native-root'}}
        self.path.write_text(json.dumps(self.original))
        installation = self.root / '.kmp-agent'
        installation.mkdir()
        (installation / 'installation.json').write_text('{}')
        (installation / 'agent.py').write_text('')
        self.crash(('applied', '.harness/tasks/tasks.yaml'))
        command = ('python3 .kmp-agent/agent.py task --run-id R '
                   '--operation recover-contract-transition --data docs/recovery.json')
        payload = {'hook_event_name': 'PreToolUse', 'cwd': str(self.root),
                   'session_id': 'native-root', 'tool_name': 'exec_command',
                   'tool_input': {'cmd': command, 'workdir': str(self.root)}}
        self.assertEqual(evaluate(self.root, payload), {})
        payload['session_id'] = 'different-root'
        denial = evaluate(self.root, payload)
        self.assertEqual(denial['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_external_edit_during_preparation_is_not_overwritten(self):
        def prepare(state):
            result = self.prepare(state)
            self.canonical.write_bytes(b'new external input')
            return result

        with self.assertRaisesRegex(ValueError, 'changed during preparation'):
            transaction.run(self.manager, 'T', prepare)
        self.assertEqual(self.canonical.read_bytes(), b'new external input')
        self.assertEqual(self.path.read_bytes(), self.before[1])
        identity = transaction.unresolved(self.root)[0]['transaction_id']
        transaction.recover(self.manager, identity, 'discard unstarted publication')
        self.assertEqual(self.canonical.read_bytes(), b'new external input')

    def test_external_edit_during_publication_prevents_commit(self):
        def edit(phase, path):
            if (phase, path) == ('applied', '.harness/tasks/tasks.yaml'):
                self.canonical.write_bytes(b'external edit before commit')

        with self.assertRaisesRegex(ValueError, 'changed before commit'):
            transaction.run(self.manager, 'T', self.prepare, fault_hook=edit)
        pending = transaction.unresolved(self.root)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.flushed, [])
        with self.assertRaisesRegex(ValueError, 'unknown edits'):
            transaction.recover(self.manager, pending[0]['transaction_id'], 'restore')
        self.assertEqual(self.canonical.read_bytes(), b'external edit before commit')


if __name__ == '__main__':
    unittest.main()
