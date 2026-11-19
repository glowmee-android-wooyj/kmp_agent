"""Populated-cache and fresh native authorization regression, synthetic only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness import decisions, logging
from harness.execution_policy import pending, pending_scopes
from harness.tasks import Tasks


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True)

    def test_prunes_generated_ignored_trees_but_preserves_tracked_and_unignored_sources(self):
        self.git('init', '-q')
        (self.root/'.gitignore').write_text('build/\nnode_modules/\ntools/.runtime/\n')
        for name in ('build/cache.bin', 'node_modules/pkg/cache.js', 'tools/.runtime/cache.bin',
                     'nested/build/Tracked.kt', 'other/dist/source.py', 'src/App.kt'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
        self.git('add', '-f', 'nested/build/Tracked.kt')
        original=Path.read_bytes
        forbidden={'build/cache.bin','node_modules/pkg/cache.js','tools/.runtime/cache.bin'}
        def read(path):
            if path.is_relative_to(self.root) and path.relative_to(self.root).as_posix() in forbidden:
                raise AssertionError('generated cache was read: '+str(path))
            return original(path)
        with patch.object(Path,'read_bytes',read):
            result=logging.snapshot(self.root,prune_generated=True)
        self.assertTrue({'nested/build/Tracked.kt','other/dist/source.py','src/App.kt'} <= set(result))
        self.assertFalse(forbidden & set(result))

    def test_prunes_old_exclusions_before_descending_and_never_follows_links(self):
        for folder in ('.harness','verification','.git','.kmp-agent/releases','__pycache__'):
            path=self.root/folder/'deep';path.mkdir(parents=True);(path/'private').write_text('ignored')
        (self.root/'source').write_text('source')
        (self.root/'linked').symlink_to(self.root/'source')
        original=os.scandir
        def scan(path):
            if Path(path) != self.root and any(p in {'.harness','verification','.git','releases','__pycache__'} for p in Path(path).relative_to(self.root).parts):
                raise AssertionError('excluded subtree was traversed')
            return original(path)
        with patch('os.scandir',side_effect=scan):
            self.assertEqual(set(logging.snapshot(self.root)),{'source'})

    def test_legacy_recovery_basis_keeps_original_cache_fingerprints(self):
        from types import SimpleNamespace
        from harness.recovery import workspace_basis, _safe_basis
        self.git('init','-q');(self.root/'.gitignore').write_text('build/\n')
        (self.root/'build').mkdir();cache=self.root/'build/cache';cache.write_text('original')
        manager=SimpleNamespace(root=self.root);task={'inputs':[]}
        legacy=workspace_basis(manager,task,reference={})
        self.assertIn('build/cache',legacy['files'])
        self.assertNotIn('scan_policy',legacy)
        new=workspace_basis(manager,task)
        self.assertNotIn('build/cache',new['files'])
        self.assertEqual(new['scan_policy'],'generated_pruned_v1')
        current,reason=_safe_basis(manager,{},task,{'recovery_start_basis':legacy})
        self.assertIsNotNone(current);self.assertIsNone(reason)
        cache.write_text('changed')
        current,reason=_safe_basis(manager,{},task,{'recovery_start_basis':legacy})
        self.assertIsNone(current);self.assertIn('workspace changed',reason)

    def test_legacy_run_checkpoint_does_not_invent_generated_file_deletion(self):
        self.git('init','-q');(self.root/'.gitignore').write_text('build/\n')
        (self.root/'build').mkdir();(self.root/'build/cache').write_text('original')
        logger=logging.Logger(self.root)
        logger.append('run_started','legacy-run','HARNESS_BUILD',
                      observed={'files':logging.snapshot(self.root,prune_generated=False)})
        checkpoint=logger.checkpoint('legacy-run','Synthetic legacy continuity')
        self.assertFalse(checkpoint['observed']['changed_files'])

    def test_verification_ownership_remains_strict_inside_ignored_generated_paths(self):
        from types import SimpleNamespace
        from harness.verification import submission_gate
        self.git('init','-q');(self.root/'.gitignore').write_text('build/\n')
        (self.root/'build').mkdir();cache=self.root/'build/cache';cache.write_text('original')
        before=logging.snapshot(self.root)
        self.assertIn('build/cache',before)  # Default remains the legacy strict boundary.
        task={'verification_contract':{'writable_paths':['reports'],'forbidden_paths':['build']}}
        manager=SimpleNamespace(root=self.root);run={'ownership_files_start':before}
        submission_gate(manager,task,run,[])
        cache.write_text('forbidden modification')
        with self.assertRaisesRegex(ValueError,'forbidden scope'):
            submission_gate(manager,task,run,[])

    def test_legacy_sequential_observation_keeps_its_opening_file_scope(self):
        from types import SimpleNamespace
        from harness.sequential import finish
        self.git('init','-q');(self.root/'.gitignore').write_text('build/\n')
        (self.root/'build').mkdir();(self.root/'build/cache').write_text('original')
        run={'collection':{'file_scope':'logger_snapshot_exclusions',
                          'files_start':logging.snapshot(self.root), 'module_map':'UNKNOWN'}}
        finish(SimpleNamespace(root=self.root),{},run)
        self.assertFalse(run['collection']['changed_files'])
        self.assertIn('build/cache',run['collection']['files_end'])

    def test_no_git_enumeration_failure_can_hide_sources(self):
        (self.root/'build').mkdir();(self.root/'build/source').write_text('keep')
        result=logging.snapshot(self.root,prune_generated=True)
        self.assertIn('build/source',result)

    def test_worker_deadline_bounds_event_and_state_lock_waits(self):
        import fcntl
        import threading
        from harness.command_preflight import Preflight, PreflightTimeout
        from harness.tracking_io import bounded_flock, state_lock
        for kind in ('events','state'):
            with self.subTest(kind=kind):
                lock_path=self.root/(kind+'.lock')
                errors=[]
                def work():
                    try:
                        with Preflight(self.root,.05):
                            if kind=='events':
                                with lock_path.open('a+') as handle:bounded_flock(handle)
                            else:
                                with state_lock(self.root,lock_path,'synthetic'):pass
                    except Exception as error:errors.append(error)
                with lock_path.open('a+') as owner:
                    fcntl.flock(owner,fcntl.LOCK_EX)
                    thread=threading.Thread(target=work);thread.start();thread.join(1)
                    self.assertFalse(thread.is_alive(),'worker lock wait exceeded its deadline')
                self.assertEqual(len(errors),1)
                self.assertIsInstance(errors[0],PreflightTimeout)

    def test_worker_git_preflight_expiry_is_not_reclassified_as_invalid_approval(self):
        import sys
        import threading
        from harness.command_preflight import Preflight, PreflightTimeout
        from harness.tracking_io import run_git
        original=subprocess.Popen
        errors=[]
        def slow_git(argv,**kwargs):
            return original([sys.executable,'-c','import time; time.sleep(2)'],**kwargs)
        def work():
            try:
                with Preflight(self.root,.05):
                    run_git(self.root,'status')
            except Exception as error:errors.append(error)
        with patch('harness.tracking_io.subprocess.Popen',side_effect=slow_git):
            thread=threading.Thread(target=work);thread.start();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],PreflightTimeout)
        records=[json.loads(line) for path in (self.root/'.harness/logs/command-preflight').glob('*.jsonl') for line in path.read_text().splitlines()]
        self.assertTrue(any(row['state']=='TIMED_OUT' for row in records))
        self.assertFalse(any(row['state']=='FAILED' for row in records))

    def test_native_batch_reuses_parsing_but_reads_exact_bytes_and_appends_again(self):
        session='00000000-0000-4000-8000-000000000913'
        path=self.root/'native.jsonl'
        rows=[{'type':'session_meta','payload':{'id':session,'source':'cli'}},
              {'type':'event_msg','payload':{'message':'safeA'}}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with patch('harness.decisions.native_store',return_value=self.root), decisions.native_record_batch(), patch('harness.decisions.json.loads',wraps=json.loads) as parse:
            decisions.native_records(path,session);decisions.native_records(path,session)
            self.assertEqual(parse.call_count,2)
            before=path.stat();path.write_bytes(path.read_bytes().replace(b'safeA',b'safeB'))
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            records,_=decisions.native_records(path,session)
            self.assertEqual(records[1]['payload']['message'],'safeB')
            self.assertEqual(parse.call_count,4)
            with path.open('a') as stream:stream.write(json.dumps({'type':'event_msg','payload':{'message':'new'}})+'\n')
            records,_=decisions.native_records(path,session)
            self.assertEqual(len(records),3)
            self.assertEqual(parse.call_count,7)
        # No parsed-data reuse leaks into another query.
        with patch('harness.decisions.native_store',return_value=self.root), patch('harness.decisions.json.loads',wraps=json.loads) as parse:
            decisions.native_records(path,session)
            self.assertEqual(parse.call_count,3)

    def test_irrelevant_answers_are_not_validated_and_dependency_questions_still_block(self):
        state={'tasks':{'A':{},'B':{},'C':{'dependencies':['A']}},'required_questions':{
            'QA':{'id':'QA','status':'ANSWERED','affected_task_ids':['A']},
            'QB':{'id':'QB','status':'ANSWERED','affected_task_ids':['B']}}}
        with patch('harness.decisions.is_superseded',return_value=False), patch('harness.decisions.validate_answer',return_value=None) as validate:
            self.assertEqual(pending(state,'C'),['QA'])
            self.assertEqual([call.args[1]['id'] for call in validate.call_args_list],['QA'])
        state['required_questions']['BROKEN']={'id':'BROKEN','status':'OPEN'}
        with patch('harness.decisions.is_superseded',return_value=False), patch('harness.decisions.validate_answer',return_value=None):
            self.assertIn('BROKEN',pending_scopes(state,'B'))


if __name__ == '__main__':unittest.main()
