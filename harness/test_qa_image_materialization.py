"""Synthetic image transport through real QA isolation; no visual-model PASS claim."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from harness.execution_plan import attach_existing
from harness.roles import Roles
from harness.test_qa_images import QAImageTests as Fixture, PNG


class QAImageMaterializationTests(unittest.TestCase):
    setUp = Fixture.setUp

    def capture(self):
        path = self.root / 'verification/qa/PREVIOUS/capture.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG)
        return path

    def configure(self, image, **configuration):
        return self.qa.configure('QA', 'design', 'TARGET', ['request.md'],
            [{'id':'Q', 'required':True, 'description':'Review registered capture'}],
            images=[str(image.relative_to(self.root))], **configuration)

    def worker(self, workspace, packet):
        proof = workspace / 'qa-tools/proof.txt'
        proof.write_text('Synthetic transport fixture; no actual image understanding')
        return {'criteria':[{'criterion_id':'Q','result':'PASS','observation':'fixture inspected',
            'evidence':'qa-tools/proof.txt'}], 'findings':[], 'reviewed_issue_ids':[],
            'recommendation':'PASS','summary':'Synthetic transport only'}

    def test_prior_verification_capture_reaches_actual_native_image_argument(self):
        image = self.capture(); config = self.configure(image)
        original = image.read_bytes()
        attach_existing(self.tasks, ['TARGET'], 'HARNESS_BUILD', 'local_change_validation',
                        'Synthetic isolated image transport', 'request.md', 'Explicit fixture plan')
        binary = self.root / 'synthetic-image-review.py'
        binary.write_text('#!' + sys.executable + '\n' + '''import hashlib,json,pathlib,sys
packet=json.load(sys.stdin)
workspace=pathlib.Path.cwd()
image=pathlib.Path(sys.argv[sys.argv.index('--image')+1])
assert image.is_file() and image.is_relative_to(workspace)
assert image.parent == workspace/'qa-tools/image-inputs'
proof=packet['image_materializations'][0]
assert image == workspace/proof['path']
assert proof['source_path'] == 'verification/qa/PREVIOUS/capture.png'
assert hashlib.sha256(image.read_bytes()).hexdigest() == proof['source_sha256'] == proof['sha256']
assert not (workspace/'verification').exists()
(workspace/'qa-tools/received.json').write_text(json.dumps({'argv':sys.argv,'packet':packet}))
(workspace/'qa-tools/proof.txt').write_text('Synthetic adapter read original image bytes')
result={'criteria':[{'criterion_id':'Q','result':'PASS','observation':'Synthetic image bytes read','evidence':'qa-tools/proof.txt','platform_results':[]}],'findings':[],'reviewed_issue_ids':[],'recommendation':'PASS','summary':'Synthetic image transport only'}
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
''')
        binary.chmod(0o700)
        with patch.object(Roles, 'collect_codex', return_value={'usage':'UNKNOWN'}):
            attempt = self.qa.execute('QA', executable=str(binary))
        self.assertEqual(attempt['outcome'], 'PASS', attempt)
        report = json.loads((self.root / attempt['report']['path']).read_text())
        workspace = (self.root / attempt['report']['path']).parent / 'workspace'
        received = json.loads((workspace / 'qa-tools/received.json').read_text())
        materialized = report['image_materializations'][0]
        self.assertEqual(materialized['source_sha256'], config['images'][0]['sha256'])
        self.assertEqual(received['argv'][received['argv'].index('--image')+1],
                         str(workspace / materialized['path']))
        attached = json.loads((workspace.parent / 'runtime/attached-images.json').read_text())
        self.assertEqual(attached, [{'path':materialized['path'], 'sha256':hashlib.sha256(original).hexdigest()}])
        self.assertEqual(image.read_bytes(), original)
        self.assertEqual(self.tasks.read()['tasks']['QA']['qa_config']['images'], config['images'])

    def test_changed_excluded_capture_is_rejected_before_worker(self):
        image = self.capture(); self.configure(image)
        image.write_bytes(PNG + b'changed after configure')
        with self.assertRaises(ValueError), patch('harness.delegation_runtime.run_codex') as native:
            self.qa.execute('QA', worker=lambda *_: self.fail('changed capture reached worker'))
        native.assert_not_called()

    def test_missing_outside_traversal_absolute_and_symlink_images_are_rejected(self):
        image = self.capture()
        link = self.root / 'verification/qa/link.png'; link.symlink_to(image)
        outside = self.root.parent / (self.root.name + '-outside.png'); outside.write_bytes(PNG)
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        paths = ['verification/missing.png', '../' + outside.name,
                 str(image), 'verification/qa/PREVIOUS/../PREVIOUS/capture.png',
                 str(link.relative_to(self.root))]
        for index, path in enumerate(paths):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.qa.configure('BAD' + str(index), 'design', 'TARGET', ['request.md'],
                    [{'id':'Q','required':True,'description':'No invalid input'}], images=[path])

    def test_symlink_swap_after_registration_is_rejected(self):
        image = self.capture(); self.configure(image)
        image.unlink(); image.symlink_to(self.root / 'design/capture.png')
        with self.assertRaises(ValueError):
            self.qa.execute('QA', worker=lambda *_: self.fail('symlink reached worker'))

    def test_isolated_copy_tampering_blocks_qa_and_preserves_source(self):
        image = self.capture(); self.configure(image)
        def tamper(workspace, packet):
            copy = workspace / packet['image_materializations'][0]['path']
            copy.write_bytes(PNG + b'tampered copy')
            return self.worker(workspace, packet)
        attempt = self.qa.execute('QA', worker=tamper)
        self.assertEqual(attempt['outcome'], 'BLOCKED')
        self.assertEqual(image.read_bytes(), PNG)

    def test_copy_corruption_before_dispatch_never_reaches_worker(self):
        from harness.qa import _materialize_images
        image = self.capture(); self.configure(image); called = []
        def corrupt(root, workspace, refs):
            copies = _materialize_images(root, workspace, refs)
            (workspace / copies[0]['path']).write_bytes(PNG + b'corrupt before dispatch')
            return copies
        def worker(workspace, packet):
            called.append(True)
            return self.worker(workspace, packet)
        with patch('harness.qa._materialize_images', side_effect=corrupt):
            self.assertEqual(self.qa.execute('QA', worker=worker)['outcome'], 'BLOCKED')
        self.assertFalse(called)
        self.assertEqual(image.read_bytes(), PNG)

    def test_materialization_never_overwrites_a_copied_source_directory(self):
        image = self.capture(); self.configure(image); called = []
        existing = self.root / 'qa-tools/image-inputs/source.txt'
        existing.parent.mkdir(parents=True)
        existing.write_text('preserved existing source')
        def worker(workspace, packet):
            called.append(True)
            return self.worker(workspace, packet)
        self.assertEqual(self.qa.execute('QA', worker=worker)['outcome'], 'BLOCKED')
        self.assertFalse(called)
        self.assertEqual(existing.read_text(), 'preserved existing source')

    def test_materialization_refuses_a_symlinked_destination_parent(self):
        from harness.qa import _materialize_images
        image = self.capture(); config = self.configure(image)
        workspace = self.root / 'isolated-image-workspace'; workspace.mkdir()
        outside = self.root / 'outside-image-workspace'; outside.mkdir()
        (workspace / 'qa-tools').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            _materialize_images(self.root, workspace, config['images'])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(image.read_bytes(), PNG)

    def test_validation_command_cannot_mutate_registered_image_copies(self):
        image = self.capture()
        self.configure(image, commands=[[sys.executable, '-c',
            'from pathlib import Path; p=next(Path("qa-tools/image-inputs").glob("*.png")); p.write_bytes(p.read_bytes()+b"modified by command")']])
        self.assertEqual(self.qa.execute('QA', worker=self.worker)['outcome'], 'BLOCKED')
        self.assertEqual(image.read_bytes(), PNG)

    def test_validation_command_changing_excluded_original_is_blocked(self):
        image = self.capture()
        self.configure(image, commands=[[sys.executable, '-c',
            'from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b"changed original by command")',
            str(image)]])
        attempt = self.qa.execute('QA', worker=self.worker)
        self.assertEqual(attempt['outcome'], 'BLOCKED')
        report = json.loads((self.root / attempt['report']['path']).read_text())
        self.assertIn('target code/baseline changed during QA', report['problems'])
        self.assertEqual(self.tasks.read()['tasks']['QA']['state'], 'BLOCKED')

    def test_excluded_capture_mutation_during_qa_invalidates_source(self):
        image = self.capture(); self.configure(image)
        def concurrent_change(workspace, packet):
            image.write_bytes(PNG + b'concurrent change')
            return self.worker(workspace, packet)
        self.assertEqual(self.qa.execute('QA', worker=concurrent_change)['outcome'], 'BLOCKED')


del Fixture
