"""Synthetic native receipt regression tests; no product records or commands."""
import copy
import hashlib
import json
import unittest

from harness import test_lineage_approval_progress as fixtures
from harness import test_project_contract as project_fixtures
from harness.requirement_lineage import file_ref, approval_subject, compare, load
from harness.development_constraints import KEY, load as constraints
from harness.development_tracking import baseline, ensure
from harness.decisions import validate_answer


class ApprovedContractTransition(unittest.TestCase):
    _fixture_setup = fixtures.LineageApprovalProgressTests._fixture_setup
    git = fixtures.LineageApprovalProgressTests.git
    step = fixtures.LineageApprovalProgressTests.step
    write_native = fixtures.LineageApprovalProgressTests.write_native
    activate = fixtures.LineageApprovalProgressTests.activate
    reply = fixtures.LineageApprovalProgressTests.reply
    plan = fixtures.LineageApprovalProgressTests.plan
    call = fixtures.LineageApprovalProgressTests.call
    structured = fixtures.LineageApprovalProgressTests.structured
    entry = fixtures.LineageApprovalProgressTests.entry
    approve = fixtures.LineageApprovalProgressTests.approve

    def setUp(self):
        fixtures.LineageApprovalProgressTests.setUp(self)
        self.policy = {'settings':{'implementation_targets':['test'], 'ui_technology':'shared fixture',
            'database':None, 'server_mode':'none', 'code_ownership':'shared'},
            'ui_sharing':'shared', 'ui_owners':{'src':'kmp'},
            'platform_entrypoints':{'test':['src/App.kt']},
            'target_commands':{'test':[['python3','-c','print("synthetic check")']]},
            'source_rules':[{'glob':'src/App.kt','required_patterns':['Old'], 'forbidden_patterns':[]}]}
        for name in ('original.md','approved.md','derived.md'):
            (self.root/name).write_text('Synthetic '+name)
        self.old = {'schema_version':1,'scope':'PRODUCT','purpose':'synthetic technical policy',
            'sources':[dict(id=i,kind=k,anchor='fixture',**file_ref(self.root,p)) for i,k,p in
                [('U','USER_ORIGINAL','original.md'),('A','APPROVED_REQUIREMENTS','approved.md'),('D','AGENT_DERIVED','derived.md')]],
            'requirements':[{'id':'TECH','source':'A','anchor':'fixture','expected':{KEY:self.policy}}],
            'derived_items':[{'id':'PLAN','source':'D','anchor':'fixture','source_ids':['TECH'],'expected':{KEY:copy.deepcopy(self.policy)}}],
            'decisions':[]}
        self.old['sources'][1]['approval']={'source':'U','anchor':'fixture','approved_sha256':self.old['sources'][1]['sha256']}
        self.write('requirement-lineage.json',self.old)
        settings = project_fixtures.ProjectContract.settings(self,'none')
        for k,v in self.policy['settings'].items(): settings[k]['value']=v
        # Database is explicitly N/A in no-server project settings.
        self.write('settings.json',settings)
        (self.root/'contract.md').write_text('Synthetic frozen project documents')
        # Existing fixture task is inactive; temporarily park its preconfigured tracking.
        self.m.configure_stage('T','ui','settings.json')
        self.m.ready('T'); self.work=self.m.begin('T','improvement')
        self.after=copy.deepcopy(self.old)
        self.after['purpose']='approved replacement policy'
        self.after['requirements'][0]['expected'][KEY]['source_rules'][0]['required_patterns']=['New']
        self.after['derived_items'][0]['expected']=copy.deepcopy(self.after['requirements'][0]['expected'])
        self.after['human_authorization']={'question_id':'T2','task_id':'T'}
        self.q=self.m.open_question('T2','Approve exact synthetic replacement',['T','U'],
            [{'id':'approve','label':'Approve this exact scope'},{'id':'reject','label':'Reject'}],
            approval_purpose='requirement_lineage', approval_subject=approval_subject(self.after),
            approval_artifacts=[s['path'] for s in self.after['sources']])
        self.approve(self.q)
        self.write('candidate.json',self.after)
        self.before_sha=hashlib.sha256((self.root/'requirement-lineage.json').read_bytes()).hexdigest()

    def write(self,name,value):
        (self.root/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

    def transition(self,**changes):
        args=dict(task_id='T',agent_run_id=self.work['agent_run_id'],question_id='T2',
            candidate='candidate.json',before_sha256=self.before_sha,reason='exact approved revision',evidence='scope.md')
        args.update(changes)
        return self.m.transition_approved_contract(**args)

    def test_preserves_native_contract_checkpoint_attempt_and_opening_baseline(self):
        before=self.m.read(); task=before['tasks']['T']; opening=copy.deepcopy(task['executions'][0])
        frozen=self.native.read_bytes(),(self.root/self.q['checkpoint']['snapshot']).read_bytes()
        self.transition()
        state=self.m.read(); current=state['tasks']['T']
        self.assertEqual(state['required_questions'],before['required_questions'])
        self.assertEqual(state['tasks']['U'],before['tasks']['U'])
        self.assertEqual(current['executions'][0],opening)
        self.assertIsNotNone(validate_answer(state,'T2',self.root))
        self.assertEqual(frozen,(self.native.read_bytes(),(self.root/self.q['checkpoint']['snapshot']).read_bytes()))
        self.assertNotEqual(opening['baseline'],baseline(current))
        constraints(self.m,current)
        self.m.resume('T',self.work['agent_run_id'],'scope.md')
        current=self.m.read()['tasks']['T']
        self.assertEqual(current['state'],'IN_PROGRESS')
        self.assertEqual(len(current['executions']),1)
        ensure(self.m,self.m.read(),current,'dispatch',current['executions'][0])

    def test_transition_proof_survives_event_flush_before_outbox_clear(self):
        link = self.transition()
        state = self.m.read()
        event = next(event for event in self.m.logger.read()
                     if event.get('source_key') == 'contract-transition:' + link['id'])
        state['outbox'].append(event)
        self.assertIsNotNone(validate_answer(state, 'T2', self.root))

    def test_transition_can_submit_and_begin_review_on_preserved_attempt(self):
        self.transition()
        self.m.resume('T',self.work['agent_run_id'],'scope.md')
        (self.root/'src').mkdir(exist_ok=True)
        (self.root/'src/App.kt').write_text('New synthetic source')
        self.m.submit('T',self.work['agent_run_id'],['src/App.kt'],'synthetic revised work')
        review=self.m.begin('T','verification',review=True)
        task=self.m.read()['tasks']['T']
        self.assertEqual(task['state'],'IN_REVIEW')
        self.assertEqual(task['executions'][0]['attempt_id'],self.work['attempt_id'])
        self.assertEqual(review['baseline'],baseline(task))

    def test_exact_native_shared_approved_and_derived_source_is_supported(self):
        after=copy.deepcopy(self.after)
        after['sources'][2].update(path=after['sources'][1]['path'],sha256=after['sources'][1]['sha256'])
        after['human_authorization']['question_id']='T3'
        q=self.m.open_question('T3','Approve exact shared technical document',['T','U'],
            [{'id':'approve','label':'Approve this exact scope'},{'id':'reject','label':'Reject'}],
            approval_purpose='requirement_lineage',approval_subject=approval_subject(after),
            approval_artifacts=[s['path'] for s in after['sources']],supersedes_question_id='T2')
        self.approve(q);self.write('candidate.json',after)
        self.transition(question_id='T3')
        self.assertTrue(compare(self.root)['authorization_ready'])
        self.m.resume('T',self.work['agent_run_id'],'scope.md')
        self.assertEqual(self.m.read()['tasks']['T']['state'],'IN_PROGRESS')

    def test_native_and_artifact_tampering_still_rejects_before_write(self):
        original=(self.root/'requirement-lineage.json').read_bytes()
        native=self.native.read_bytes()
        self.native.write_bytes(native.replace(b'Approve this exact scope',b'Changed approval label'))
        with self.assertRaises(ValueError):self.transition()
        self.assertEqual((self.root/'requirement-lineage.json').read_bytes(),original)
        self.native.write_bytes(native)
        (self.root/'approved.md').write_text('changed authoritative bytes')
        with self.assertRaises(ValueError):self.transition()
        self.assertEqual((self.root/'requirement-lineage.json').read_bytes(),original)

    def test_repeated_transition_does_not_mutate_original_approval(self):
        self.transition()
        original=(self.root/'requirement-lineage.json').read_bytes()
        with self.assertRaises(ValueError):self.transition()
        self.assertEqual((self.root/'requirement-lineage.json').read_bytes(),original)
        self.assertEqual(len(self.m.read()['tasks']['T']['contract_transitions']),1)

    def test_old_requirements_qa_is_preserved_but_cannot_certify_revised_contract(self):
        from harness.requirement_lineage import require_review, criteria
        self.m.register('F','OLD-QA','verification','synthetic historical QA','old contract only',
            criteria(self.root),['requirement-lineage.json'],execution_scope='documentation')
        def historical(state):
            task=state['tasks']['OLD-QA']
            task['state']='DONE'
            task['qa_config']={'stage':'requirements','target_task_id':'T',
                'attempts':[{'outcome':'PASS','agent_run_id':'historical-synthetic-review'}]}
        self.m._mutate('synthetic_historical_qa',historical)
        old=copy.deepcopy(self.m.read()['tasks']['OLD-QA'])
        self.transition()
        state=self.m.read()
        self.assertEqual(state['tasks']['OLD-QA'],old)
        self.assertEqual(old['state'],'DONE')
        self.assertNotEqual(old['inputs'][0]['sha256'],
            state['tasks']['T']['stage_contract'][KEY]['lineage']['sha256'])
        with self.assertRaisesRegex(ValueError,'requirements QA missing or stale'):
            require_review(self.m,state['tasks']['T'],allow_mock=True)

    def test_artifact_snapshot_rejects_symlink_parent_before_external_write(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from harness.contract_transition import _artifact_bytes
        with tempfile.TemporaryDirectory() as directory:
            outside=Path(directory)
            root=outside/'root';root.mkdir()
            target=outside/'outside';target.mkdir()
            (root/'.harness').symlink_to(target,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'symlink'):
                _artifact_bytes(SimpleNamespace(root=root),'candidate.json',b'proof')
            self.assertEqual(list(target.iterdir()),[])

    def test_unapproved_candidate_or_wrong_before_hash_rejected_before_write(self):
        original=(self.root/'requirement-lineage.json').read_bytes()
        for bad in ('before','subject','locator','original'):
            with self.subTest(case=bad):
                candidate=copy.deepcopy(self.after); args={}
                if bad=='before':args['before_sha256']='0'*64
                elif bad=='subject':candidate['purpose']='unapproved expansion'
                elif bad=='locator':candidate['human_authorization']['question_id']='OTHER'
                else:candidate['sources'][0]['anchor']='rewritten original'
                self.write('candidate.json',candidate)
                with self.assertRaises(ValueError):self.transition(**args)
                self.assertEqual((self.root/'requirement-lineage.json').read_bytes(),original)
                self.assertFalse(self.m.read()['tasks']['T'].get('contract_transitions'))

    def test_snapshot_lock_symlink_cannot_create_external_file(self):
        from harness.contract_transition import _artifact_bytes
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as outside:
            target=Path(outside)/'must-not-exist'
            lock=self.root/'.harness/artifacts/snapshot.lock'
            if lock.exists():lock.unlink()
            lock.symlink_to(target)
            with self.assertRaisesRegex(ValueError,'lock cannot be symlink'):
                _artifact_bytes(self.m,'synthetic.json',b'synthetic new snapshot')
            self.assertFalse(target.exists())

    def test_transition_proof_and_other_affected_task_remain_strict(self):
        self.transition(); original=self.m.read()
        for kind in ('other_scope','scope','proof','inputs','baseline'):
            state=copy.deepcopy(original)
            if kind=='other_scope':state['tasks']['U']['scope']='expanded'
            elif kind=='scope':state['tasks']['T']['scope']='expanded'
            elif kind=='proof':state['tasks']['T']['contract_transitions'][0]['reason']='forged'
            elif kind=='inputs':state['tasks']['T']['inputs']=[]
            else:state['tasks']['T']['development_tracking']['code_paths']=['outside']
            with self.subTest(kind=kind):self.assertIsNone(validate_answer(state,'T2',self.root))

    def test_nontechnical_mutation_rejected_even_if_exactly_approved(self):
        from harness.contract_transition import _technical_only
        before=copy.deepcopy(self.old);after=copy.deepcopy(self.after)
        before['requirements'].append({'id':'FEATURE','source':'U','anchor':'feature','expected':{'feature':'unchanged'}})
        after['requirements'].append({'id':'FEATURE','source':'U','anchor':'feature','expected':{'feature':'expanded'}})
        with self.assertRaisesRegex(ValueError,'nontechnical'):_technical_only(before,after)

    def test_approved_dual_technical_source_never_promotes_agent_source_to_user_original(self):
        from harness.contract_transition import _technical_only
        after=copy.deepcopy(self.after)
        after['sources'][2].update(path=after['sources'][1]['path'],sha256=after['sources'][1]['sha256'])
        # The original q did not approve the shared-source subject.
        with self.assertRaisesRegex(ValueError,'exact native'):load(self.root,after,state=self.m.read())
        after['sources'][2]['kind']='USER_ORIGINAL'
        with self.assertRaisesRegex(ValueError,'exact native'):load(self.root,after,state=self.m.read())


if __name__ == '__main__':unittest.main()
