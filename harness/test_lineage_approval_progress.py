"""Synthetic v1 native approvals in disposable Git roots; no product authority."""
import copy
import subprocess
import sys
import unittest
from unittest.mock import patch

from harness.approvals import human_approval
from harness.decisions import digest, validate_answer
from harness.execution_policy import managed_command, pending
from harness.test_approval_transport import ApprovalTransportTests as Transport
from harness.test_execution_policy import ExecutionPolicyTests as Fixture


class LineageApprovalProgressTests(unittest.TestCase):
    _fixture_setup = Fixture.setUp
    step = Fixture.step
    write_native = Fixture.write_native
    activate = Fixture.activate
    reply = Fixture.reply
    plan = Fixture.plan
    call = Transport.call
    structured = Transport.structured
    entry = Transport.entry

    def setUp(self):
        self._fixture_setup()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        (self.root / '.gitignore').write_text('.harness/\nhome/\n')
        self.git('add', '.gitignore', 'scope.md')
        self.git('commit', '-qm', 'Synthetic empty implementation')
        (self.root / 'technical.md').write_text('Immutable technology and ownership')
        self.m.configure_development('T', {'planning': 'scope.md'}, code_paths=['src'])
        self.activate()
        self.plan()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def question(self, qid='Q', purpose='requirement_lineage', **extra):
        return self.m.open_question(qid, 'Authorize immutable synthetic source and scope', ['T'],
            [{'id': 'approve', 'label': 'Approve this exact scope'},
             {'id': 'reject', 'label': 'Reject'}],
            approval_purpose=purpose, approval_subject={'source_ownership': 'synthetic'},
            approval_artifacts=['scope.md', 'technical.md'], **extra)

    def approve(self, q):
        item = self.call(q, 'call-' + q['id'])
        proof = self.structured([self.entry(q, item)])
        return self.m.answer_question(q['id'], **proof), proof, item

    def consume(self, q, state=None, purpose=None):
        return human_approval(self.root, state or self.m.read(), q['id'], 'T',
            purpose or q['decision_contract']['purpose'],
            q['decision_contract']['subject'], q['decision_contract']['artifacts'])

    def write_code(self, name='First.kt', content='val first = 1\n'):
        path = self.root / 'src' / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)

    def test_existing_v1_receipt_survives_two_edits_and_commit_without_rewriting_proof(self):
        q = self.question()
        answered, _, _ = self.approve(q)
        checkpoint = self.root / q['checkpoint']['snapshot']
        frozen = checkpoint.read_bytes(), self.native.read_bytes()
        contract = copy.deepcopy(answered['decision_contract'])
        self.assertEqual(contract['version'], 1)
        self.assertIn('state_sha256', contract['tasks']['T']['code_basis'])
        self.m.ready('T')
        run = self.m.begin('T', 'improvement')
        self.m.require_dispatch('T')
        self.write_code()
        # This is the failing v0.4.9 boundary: the second authorized edit cannot start.
        self.m.require_dispatch('T')
        self.write_code('Second.kt', 'val second = 2\n')
        self.m.require_dispatch('T')
        self.git('add', 'src')
        self.git('commit', '-qm', 'Synthetic authorized implementation')
        self.assertIsNotNone(self.consume(q))
        command = managed_command(self.m, 'T', [sys.executable, '-c', 'print("verified")'], self.root)
        self.assertEqual((command.returncode, command.stdout.strip()), (0, 'verified'))
        current = self.m.read()
        self.assertEqual(pending(current, 'T'), [])
        self.assertEqual(current['required_questions']['Q']['decision_contract'], contract)
        self.assertEqual(current['required_questions']['Q']['answer'], answered['answer'])
        self.assertEqual((checkpoint.read_bytes(), self.native.read_bytes()), frozen)
        self.assertEqual(current['tasks']['T']['executions'][0]['agent_run_id'], run['agent_run_id'])
        self.assertEqual(len(current['tasks']['T']['executions']), 1)

    def test_checkpointed_attempt_resumes_with_existing_approval_after_code_change(self):
        self.m.ready('T')
        run = self.m.begin('T', 'improvement')
        q = self.question()
        self.approve(q)
        self.write_code()
        before = self.m.read()['tasks']['T']
        self.assertEqual(before['state'], 'BLOCKED')
        self.assertEqual(before['executions'][0]['required_question_wait'], 'Q')
        self.assertEqual(before['executions'][0]['checkpoint'], q['checkpoint'])
        self.m.resume('T', run['agent_run_id'], 'scope.md')
        self.m.require_dispatch('T')
        after = self.m.read()['tasks']['T']
        self.assertEqual(after['state'], 'IN_PROGRESS')
        self.assertEqual(after['max_attempts'], before['max_attempts'])
        self.assertEqual(len(after['executions']), 1)
        self.assertEqual(after['executions'][0]['attempt_id'], run['attempt_id'])
        self.assertIsNotNone(self.consume(q))
        self.assertEqual(len(self.m.read()['required_questions']), 1)

    def test_open_source_question_can_accept_original_native_answer_after_code_change(self):
        q = self.question()
        self.write_code()
        answered, _, _ = self.approve(q)
        self.assertEqual(answered['decision_contract'], q['decision_contract'])
        self.assertIsNotNone(self.consume(q))

    def test_other_typed_purposes_still_certify_exact_code_state(self):
        q = self.question(purpose='stage:development_plan')
        self.approve(q)
        self.assertIsNotNone(self.consume(q))
        self.write_code()
        self.assertIsNone(self.consume(q))
        self.assertEqual(pending(self.m.read(), 'T'), ['Q'])

    def test_changed_authority_bytes_and_frozen_inputs_still_invalidate(self):
        q = self.question()
        self.approve(q)
        for name in ('scope.md', 'technical.md'):
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(path=name):
                path.write_text('Changed authority, not implementation progress')
                self.assertIsNone(self.consume(q))
                self.assertEqual(pending(self.m.read(), 'T'), ['Q'])
            path.write_bytes(original)
        self.assertIsNotNone(self.consume(q))

    def test_changed_task_ownership_scope_or_tracking_still_invalidates(self):
        q = self.question()
        self.approve(q)
        original = self.m.read()
        for key, value in [('scope', 'expanded'), ('role', 'development'), ('feature_id', 'OTHER'),
                           ('inputs', []), ('development_tracking', None)]:
            with self.subTest(key=key):
                state = copy.deepcopy(original)
                state['tasks']['T'][key] = value
                self.assertIsNone(self.consume(q, state))
        for key, value in [('code_paths', ['src', 'other']), ('scope_policy', 'full_repository')]:
            with self.subTest(key=key):
                state = copy.deepcopy(original)
                state['tasks']['T']['development_tracking'][key] = value
                self.assertIsNone(self.consume(q, state))
        self.assertIsNone(self.consume(q, purpose='stage:development_plan'))
        self.assertIsNotNone(self.consume(q))

    def test_capture_and_native_or_checkpoint_integrity_remain_fail_closed(self):
        q = self.question()
        self.approve(q)
        self.write_code()
        path = self.root / 'src/First.kt'
        path.unlink()
        path.symlink_to(self.root / 'scope.md')
        self.assertIsNone(self.consume(q))
        path.unlink()
        self.write_code()
        self.assertIsNotNone(self.consume(q))
        with patch('harness.development_tracking.capture', side_effect=ValueError('unstable Git view')):
            self.assertIsNone(self.consume(q))
        snapshot = self.root / q['checkpoint']['snapshot']
        original = snapshot.read_bytes()
        snapshot.write_text('{}')
        self.assertIsNone(self.consume(q))
        snapshot.write_bytes(original)
        self.assertIsNotNone(self.consume(q))
        self.records[-1]['payload']['content'][0]['text'] = 'Forged native answer'
        self.write_native()
        self.assertIsNone(self.consume(q))

    def test_existing_v1_recovery_reverifies_without_changing_approval_or_snapshot(self):
        q = self.question()
        answered, proof, item = self.approve(q)
        self.write_code()
        recovered = self.m.recover_question(q['id'], reason='Reverify existing native source approval',
            expected_contract_sha256=digest(q['decision_contract']), question_item_id=item, **proof)
        self.assertEqual(recovered['answer'], answered['answer'])
        self.assertEqual(recovered['decision_contract'], q['decision_contract'])
        self.assertEqual(recovered['recovery_history'][0]['result'], 'REVERIFIED')
        self.assertIsNotNone(validate_answer(self.m.read(), 'Q', root=self.root))

    def test_supersession_preserves_old_proofs_and_remains_valid_during_progress(self):
        old = self.question('OLD')
        self.approve(old)
        old_answer = copy.deepcopy(self.m.read()['required_questions']['OLD'])
        (self.root / 'technical.md').write_text('Explicitly revised technology source')
        self.assertIsNone(self.consume(old))
        new = self.question('NEW', supersedes_question_id='OLD')
        self.assertEqual(pending(self.m.read(), 'T'), ['OLD', 'NEW'])
        self.approve(new)
        self.write_code()
        receipt = self.consume(new)
        self.assertIsNotNone(receipt)
        from harness.decisions import resolved_answer
        replacement = resolved_answer(self.m.read(), 'OLD', root=self.root)
        self.assertEqual(replacement['question_id'], 'NEW')
        self.assertEqual(replacement['supersession_trace'][0]['question_id'], 'OLD')
        self.assertEqual(pending(self.m.read(), 'T'), [])
        self.assertEqual(self.m.read()['required_questions']['OLD'], old_answer)

    def test_explicit_implementation_artifact_is_not_exempt_from_frozen_bytes(self):
        self.write_code()
        q = self.m.open_question('Q', 'Approve an explicitly frozen source file', ['T'],
            [{'id': 'approve', 'label': 'Approve this exact scope'}],
            approval_purpose='requirement_lineage', approval_subject={'source_ownership': 'synthetic'},
            approval_artifacts=['scope.md', 'technical.md', 'src/First.kt'])
        self.approve(q)
        self.assertIsNotNone(self.consume(q))
        self.write_code(content='val first = 2\n')
        self.assertIsNone(self.consume(q))


del Fixture, Transport
