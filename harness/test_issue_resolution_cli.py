"""Issue resolution through a selected installed CLI; synthetic stages only."""
import base64
import copy
import json
import unittest

from harness.stage_readiness import DOMAINS, STAGES
from harness.test_installation import InstallationTests as Fixture


class IssueResolutionCLITests(unittest.TestCase):
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    install = Fixture.install
    cli = Fixture.cli
    _installed_setup = Fixture.setUp

    def setUp(self):
        self._installed_setup()
        self.install()
        self.serial = 0
        self.run = 'SYNTHETIC-ISSUE-CLI'
        (self.target / 'req.md').write_text('Synthetic approved requirement input')

    def operation(self, name, data=None, *, ok=True):
        args = ['task', '--run-id', self.run, '--operation', name]
        if data is not None:
            self.serial += 1
            relative = 'docs/guard-inputs/operation-' + str(self.serial) + '.json'
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
            args += ['--data', relative]
        result = self.cli(*args)
        if ok:
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        else:
            self.assertNotEqual(0, result.returncode)
        return json.loads(result.stdout) if result.stdout.strip().startswith(('{', '[')) else result.stdout

    def state(self):
        return self.operation('show')

    def workflow(self):
        contract = {}
        for stage, role in [('planning', 'planning'), ('design', 'design'), ('development_plan', 'development')]:
            checks = {domain: stage + '-' + domain for domain in DOMAINS[stage]}
            self.operation('register', dict(feature_id='F', task_id=stage, role=role,
                title='Synthetic ' + stage, scope='Synthetic independent ' + stage,
                criteria=[dict(id=value, required=True, text=key) for key, value in checks.items()],
                inputs=['req.md'], max_attempts=3, work_type='PRODUCT', execution_scope='documentation'))
            contract[stage] = dict(task_id=stage, checks=checks)
        self.operation('configure-stage-workflow', dict(feature_id='F', stages=contract))

    def prepare_review(self, stage):
        state = self.state()
        task_id = state['stage_workflows']['F']['stages'][stage]['task_id']
        task = state['tasks'][task_id]
        inputs = ['req.md']
        for before in STAGES[:STAGES.index(stage)]:
            upstream = state['stage_workflows']['F']['stages'][before]['task_id']
            inputs += [ref['path'] for ref in state['tasks'][upstream]['outputs']]
        self.operation('bind-stage-inputs', dict(task_id=task_id, inputs=inputs, reason='Exact current upstream outputs'))
        if task['state'] != 'READY':
            self.operation('ready', dict(task_id=task_id))
        work = self.operation('begin', dict(task_id=task_id, role=task['role']))
        path = 'docs/' + task_id + '.md'
        (self.target / path).write_text('Synthetic owner repair output for ' + task_id)
        submission = dict(task_id=task_id, agent_run_id=work['agent_run_id'], outputs=[path],
                          summary='Synthetic role output; no actual product claims')
        if stage == 'design':
            submission['design_status'] = dict(specification='COMPLETE', mockup='NOT_PRODUCED', visual_review='NOT_RUN')
        self.operation('submit', submission)
        reviewer = self.operation('begin', dict(task_id=task_id, role='verification', review=True))
        task = self.state()['tasks'][task_id]
        evidence = 'docs/proof-' + task_id + '.json'
        report = dict(result='PASS', tested_outputs={ref['path']: ref['sha256'] for ref in task['outputs']},
                      resolved_issues=task.get('returned_issues', []), observations=['Synthetic issue-specific repaired omission'])
        (self.target / evidence).write_text(json.dumps(report))
        return task_id, work, reviewer, evidence, report

    def finish_review(self, task_id, reviewer, evidence, return_stage=None):
        task = self.state()['tasks'][task_id]
        results = [dict(criterion_id=c['id'], result='PASS', evidence=evidence) for c in task['criteria']]
        if return_stage:
            results[0].update(result='FAIL', return_stage=return_stage, reason='Synthetic upstream omission')
        self.operation('review', dict(task_id=task_id, agent_run_id=reviewer['agent_run_id'],
                                     results=results, summary='Synthetic responsible stage review'))

    def approve(self, stage, return_stage=None):
        task_id, work, reviewer, evidence, report = self.prepare_review(stage)
        self.finish_review(task_id, reviewer, evidence, return_stage)

    def returned_review(self, stage='planning'):
        self.workflow()
        self.approve('planning')
        if stage == 'planning':
            self.approve('design', return_stage='planning')
        else:
            self.approve('design')
            self.approve('development_plan', return_stage='design')
        task_id, work, reviewer, evidence, report = self.prepare_review(stage)
        issue = self.state()['tasks'][task_id]['returned_issues'][0]
        return task_id, work, reviewer, evidence, report, issue

    def test_installed_parser_accepts_resolve_issue(self):
        result = self.cli('task', '--operation', 'resolve-issue', '--help')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('resolve-issue', result.stdout)

    def test_stage_return_owner_repair_resolution_and_review_use_official_cli(self):
        task_id, work, reviewer, evidence, report, issue_id = self.returned_review()
        command = ('python3 .kmp-agent/agent.py task --run-id ' + self.run
                   + ' --operation resolve-issue --data docs/guard-inputs/resolve.json')
        payload = dict(task_id=task_id, agent_run_id=reviewer['agent_run_id'], issue_id=issue_id, evidence=evidence)
        (self.target / 'docs/guard-inputs/resolve.json').write_text(json.dumps(payload))
        guard = self.cli('guard', input=json.dumps(dict(hook_event_name='PreToolUse', cwd=str(self.target),
            session_id='synthetic-issue-cli', tool_name='exec_command', tool_input=dict(cmd=command))))
        self.assertEqual(0, guard.returncode, guard.stderr)
        self.assertEqual({}, json.loads(guard.stdout))
        self.operation('resolve-issue', payload)
        self.finish_review(task_id, reviewer, evidence)
        state = self.state()
        issue = next(i for i in state['issues'] if i['id'] == issue_id)
        self.assertEqual('RESOLVED', issue['status'])
        self.assertEqual(reviewer['agent_run_id'], issue['resolution']['review_run_id'])
        self.assertEqual('DONE', state['tasks'][task_id]['state'])
        self.assertEqual('DONE', state['tasks']['planning']['state'])  # Historical approval remains intact.
        self.approve('design')
        self.approve('development_plan')
        final = self.state()
        for stage in STAGES:
            current = final['stage_workflows']['F']['stages'][stage]['task_id']
            self.assertEqual('DONE', final['tasks'][current]['state'])

    def test_inactive_wrong_role_stale_and_non_issue_evidence_are_rejected(self):
        task_id, work, reviewer, evidence, report, issue_id = self.returned_review()
        payload = dict(task_id=task_id, agent_run_id=reviewer['agent_run_id'], issue_id=issue_id, evidence=evidence)
        rejected = self.operation('resolve-issue', dict(payload, agent_run_id=work['agent_run_id']), ok=False)
        self.assertIn('active', rejected['cause'])
        rejected = self.operation('begin', dict(task_id=task_id, role='planning', review=True), ok=False)
        self.assertIn('review role', rejected['cause'])
        self.operation('register', dict(feature_id='OTHER', task_id='WRONG-OWNER', role='design',
            title='Wrong responsible role', scope='Synthetic other design scope',
            criteria=[dict(id='OTHER', required=True)], inputs=['req.md'],
            work_type='PRODUCT', execution_scope='documentation'))
        self.operation('ready', dict(task_id='WRONG-OWNER'))
        other_work = self.operation('begin', dict(task_id='WRONG-OWNER', role='design'))
        (self.target / 'docs/other-design.md').write_text('Synthetic unrelated design')
        self.operation('submit', dict(task_id='WRONG-OWNER', agent_run_id=other_work['agent_run_id'],
            outputs=['docs/other-design.md'], summary='Synthetic unrelated output',
            design_status=dict(specification='COMPLETE', mockup='NOT_PRODUCED', visual_review='NOT_RUN')))
        other_review = self.operation('begin', dict(task_id='WRONG-OWNER', role='verification', review=True))
        rejected = self.operation('resolve-issue', dict(payload, task_id='WRONG-OWNER',
            agent_run_id=other_review['agent_run_id']), ok=False)
        self.assertIn('responsible', rejected['cause'])
        for changes, expected in [({'tested_outputs': {'stale.md': 'not-current'}}, 'exact current outputs'),
                                  ({'resolved_issues': []}, 'issue-specific'),
                                  ({'observations': []}, 'issue-specific')]:
            invalid = dict(copy.deepcopy(report), **changes)
            (self.target / evidence).write_text(json.dumps(invalid))
            rejected = self.operation('resolve-issue', payload, ok=False)
            self.assertIn(expected, rejected['cause'])
            self.assertEqual('OPEN', next(i for i in self.state()['issues'] if i['id'] == issue_id)['status'])
        (self.target / evidence).write_text(json.dumps(report))
        self.operation('resolve-issue', payload)
        self.finish_review(task_id, reviewer, evidence)
        rejected = self.operation('resolve-issue', payload, ok=False)
        self.assertIn('active', rejected['cause'])

    def test_output_edited_after_submission_cannot_resolve_against_old_hashes(self):
        task_id, work, reviewer, evidence, report, issue_id = self.returned_review()
        path = self.target / self.state()['tasks'][task_id]['outputs'][0]['path']
        original = path.read_bytes()
        outside = self.target.parent / 'outside-repair.md'
        outside.write_bytes(original)
        for kind in ('changed', 'missing', 'outside'):
            with self.subTest(kind=kind):
                if kind == 'changed':
                    path.write_text('Changed after submission; old report must not certify these bytes')
                else:
                    path.unlink()
                    if kind == 'outside':
                        path.symlink_to(outside)
                self.operation('resolve-issue', dict(task_id=task_id, agent_run_id=reviewer['agent_run_id'],
                    issue_id=issue_id, evidence=evidence), ok=False)
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.write_bytes(original)
                self.assertEqual('OPEN', next(i for i in self.state()['issues'] if i['id'] == issue_id)['status'])

    def test_design_resolution_still_requires_current_capture_artifact(self):
        task_id, work, reviewer, evidence, report, issue_id = self.returned_review('design')
        payload = dict(task_id=task_id, agent_run_id=reviewer['agent_run_id'], issue_id=issue_id, evidence=evidence)
        rejected = self.operation('resolve-issue', payload, ok=False)
        self.assertIn('actual rerender capture', rejected['cause'])
        report['screenshots'] = ['docs/missing-capture.png']
        (self.target / evidence).write_text(json.dumps(report))
        self.operation('resolve-issue', payload, ok=False)
        capture = self.target / 'docs/synthetic-rerender.png'
        capture.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        report['screenshots'] = ['docs/synthetic-rerender.png']
        (self.target / evidence).write_text(json.dumps(report))
        self.operation('resolve-issue', payload)
        resolved = next(i for i in self.state()['issues'] if i['id'] == issue_id)
        self.assertEqual('RESOLVED', resolved['status'])
        self.assertEqual('docs/synthetic-rerender.png', resolved['resolution']['captures'][0]['path'])
        self.finish_review(task_id, reviewer, evidence)


del Fixture
