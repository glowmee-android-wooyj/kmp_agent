"""Synthetic legacy snapshot recovery through admission and actual Tasks.begin.

Only the model-role adapter is replaced; no product/model acceptance is claimed.
"""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness.logging import Logger
from harness.process_ownership import TaskLease, current_lease
from harness.recovery import recover_tasks, resume_task, workspace_basis
from harness.roles import Roles
from harness.tasks import Tasks


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root/'.gitignore').write_text('.harness/\nbuild/\n')
        (self.root/'scope.md').write_text('Synthetic immutable recovery scope')
        (self.root/'build').mkdir()
        self.cache = self.root/'build/cache.bin'
        self.cache.write_text('Legacy observed cache bytes')
        logger = Logger(self.root)
        self.run_id = logger.start('HARNESS_BUILD', 'improvement', 'Synthetic legacy recovery')
        self.manager = Tasks(self.root, self.run_id)
        self.manager.register('F', 'T', 'improvement', 'Legacy recovery',
            'Synthetic fixture; no model or product acceptance',
            [{'id':'C', 'required':True, 'description':'Preserve saved observation policy'}],
            ['scope.md'], work_type='HARNESS_BUILD', max_attempts=3,
            execution_scope='documentation')
        self.manager.ready('T')
        # Model a real pre-update execution: its original basis had no marker
        # and observed ignored generated files. No stored legacy record is edited.
        def legacy_basis(manager, task, reference=None):
            return workspace_basis(manager, task, reference={})
        with patch('harness.recovery.workspace_basis', side_effect=legacy_basis):
            with TaskLease(self.root, 'T'):
                self.original = self.manager.begin('T', 'improvement')
        self.opening = copy.deepcopy(self.original['recovery_start_basis'])
        self.assertNotIn('scan_policy', self.opening)
        self.assertIn('build/cache.bin', self.opening['files'])

    def recover(self):
        # The actual owner/child inspection boundary is the only simulated
        # interruption fact. All recovery state, basis checks and admission run.
        with patch('harness.process_ownership.inspect_owner',
                   return_value={'status':'DEAD', 'reason':'Synthetic confirmed interruption'}):
            return recover_tasks(self.manager, ['T'])['tasks'][0]

    def test_legacy_recover_resume_and_actual_begin_keep_original_policy(self):
        receipt = self.recover()
        self.assertEqual(receipt['state'], 'RECOVERABLE')
        saved = json.loads((self.root/receipt['checkpoint']['snapshot']).read_text())
        self.assertNotIn('scan_policy', saved['current_basis'])
        self.assertIn('build/cache.bin', saved['current_basis']['files'])
        calls = []
        def synthetic_role_adapter(roles, task_id, role, worker=None):
            self.assertIsNotNone(current_lease(self.root, task_id))
            calls.append((task_id, role, worker))
            roles.tasks.ready(task_id)
            return roles.tasks.begin(task_id, role, execution_mode='local_check')
        with patch.object(Roles, 'execute', new=synthetic_role_adapter):
            resumed = resume_task(self.manager, 'T', worker='synthetic-adapter')
        self.assertEqual(calls, [('T','improvement','synthetic-adapter')])
        self.assertNotIn('scan_policy', resumed['recovery_start_basis'])
        self.assertEqual(resumed['recovery_start_basis'], self.opening)
        self.assertEqual(resumed['recovery_from'], receipt['recovery_id'])
        current = self.manager.read()['tasks']['T']
        self.assertEqual(current['max_attempts'], 3)
        self.assertEqual(len(current['executions']), 2)
        self.assertEqual(current['executions'][0]['outcome'], 'INTERRUPTED')
        self.assertEqual(current['executions'][0]['recovery_start_basis'], self.opening)
        self.assertEqual(current['recovery']['resumed_execution'], resumed['agent_run_id'])
        self.assertEqual(current['state'], 'IN_PROGRESS')

    def test_changed_legacy_cache_blocks_recovery_and_never_starts_role(self):
        self.cache.write_text('Changed bytes were not reviewed or approved')
        receipt = self.recover()
        self.assertEqual(receipt['state'], 'RECHECK_REQUIRED')
        self.assertIn('workspace changed', receipt['reason'])
        with patch.object(Roles, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'explicit recoverable checkpoint'):
                resume_task(self.manager, 'T', worker='synthetic-adapter')
        execute.assert_not_called()
        current = self.manager.read()['tasks']['T']
        self.assertEqual(len(current['executions']), 1)
        self.assertEqual(current['executions'][0]['recovery_start_basis'], self.opening)

    def test_legacy_cache_changed_after_checkpoint_blocks_resume_admission(self):
        receipt = self.recover()
        self.assertEqual(receipt['state'], 'RECOVERABLE')
        self.cache.write_text('Changed after recovery checkpoint')
        with patch.object(Roles, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'recovery basis changed'):
                resume_task(self.manager, 'T', worker='synthetic-adapter')
        execute.assert_not_called()
        current = self.manager.read()['tasks']['T']
        self.assertEqual(len(current['executions']), 1)
        self.assertNotIn('resume_dispatched', current['recovery'])


if __name__ == '__main__':
    unittest.main()
