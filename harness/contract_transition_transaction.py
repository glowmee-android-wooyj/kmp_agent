"""Durable canonical-contract/ledger transaction with explicit crash recovery.

The shared integration lock prevents observers from reading a half transition.
An abandoned journal blocks reads until explicit recovery; recovery never
overwrites bytes that are not one of the transaction's pinned versions.
"""
import copy
import hashlib
import json
from pathlib import Path
import stat
import uuid

from harness import integration_transaction as integration

TERMINAL = {'COMMITTED', 'ROLLED_BACK'}


def _base(root):
    root = Path(root).resolve()
    base = root / '.harness' / 'contract-transition-transactions'
    for path in (root / '.harness', base):
        if path.is_symlink():
            raise ValueError('contract transition journal cannot contain symlinks')
    return base


def _target(root, name):
    if name not in {'requirement-lineage.json', '.harness/tasks/tasks.yaml'}:
        raise ValueError('contract transition may only write canonical lineage and task ledger')
    root = Path(root).resolve()
    target = root / name
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink():
            raise ValueError('contract transition target cannot contain symlinks')
    if not target.is_file():
        raise ValueError('contract transition requires existing canonical lineage and task ledger')
    return target


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(folder):
    if folder.is_symlink() or (folder / 'journal.json').is_symlink():
        raise ValueError('unsafe contract transition journal')
    data = json.loads((folder / 'journal.json').read_text())
    if (data.get('schema_version') != 1 or data.get('transaction_id') != folder.name
            or data.get('state') not in TERMINAL | {'STAGING', 'PREPARED', 'APPLYING', 'ROLLING_BACK'}
            or not isinstance(data.get('files'), list)
            or (data.get('staging_only') and
                (data['state'] != 'ROLLED_BACK' or data['files']))
            or (data['state'] != 'STAGING' and not data.get('staging_only') and
                ({row.get('path') for row in data['files']} !=
                 {'requirement-lineage.json', '.harness/tasks/tasks.yaml'} or len(data['files']) != 2))
            or (data['state'] == 'STAGING' and data['files'])):
        raise ValueError('invalid contract transition journal')
    for row in data['files']:
        if type(row.get('mode')) is not int or not 0 <= row['mode'] <= 0o7777:
            raise ValueError('invalid contract transition target mode')
        for side in ('before', 'after'):
            sha = row.get(side + '_sha256')
            if (not isinstance(sha, str) or len(sha) != 64
                    or any(c not in '0123456789abcdef' for c in sha)):
                raise ValueError('invalid contract transition snapshot hash')
    return data


def unresolved(root):
    result = []
    base = _base(root)
    if not base.exists():
        return result
    for folder in sorted(base.glob('txn-*')):
        try:
            record = _read(folder)
        except (ValueError, OSError, TypeError, KeyError):
            record = {'state': 'MANUAL', 'transaction_id': folder.name}
        if record['state'] not in TERMINAL:
            result.append({'transaction_id': record['transaction_id'], 'state': record['state']})
    return result


def _snapshot(folder, row, side):
    # Snapshot names are derived, never supplied by the journal.
    index = 0 if row['path'] == 'requirement-lineage.json' else 1
    path = folder / f'{index}-{side}.bin'
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or unsafe contract transition snapshot')
    value = path.read_bytes()
    if _sha(value) != row[side + '_sha256']:
        raise ValueError('contract transition snapshot changed')
    return value


def run(manager, task_id, prepare, *, fault_hook=None):
    """Prepare under both locks, then durably publish canonical bytes and ledger.

    prepare(state) returns (result, canonical_path, canonical_bytes). It must
    only modify the supplied candidate state and create immutable artifacts.
    Test-only fault_hook is never selected by project configuration.
    """
    from harness.tracking_io import state_lock
    from harness.protected_verification import validate_ledger
    from harness.development_tracking import refresh
    from harness.verification import refresh as refresh_verification
    from harness.decisions import refresh_waits
    from harness.execution_policy import gate
    from harness.recovery import progress_gate

    with integration._locked(manager.root, kind='contract_transition'):
        if integration.unresolved(manager.root) or unresolved(manager.root):
            raise ValueError('unresolved transaction; explicit recovery required')
        with state_lock(manager.root, manager.directory / 'state.lock', 'approved_contract_transition'):
            ledger = _target(manager.root, '.harness/tasks/tasks.yaml')
            state = json.loads(ledger.read_text())
            validate_ledger(manager.root, state)
            manager._flush(state)
            canonical = _target(manager.root, 'requirement-lineage.json')
            before_canonical, before_ledger = canonical.read_bytes(), ledger.read_bytes()
            refresh(manager, state)
            refresh_verification(manager, state)
            refresh_waits(manager, state)
            original = copy.deepcopy(state)
            try:
                progress_gate(manager, task_id, 'approved_contract_transition')
                gate(manager, state, task_id, 'approved_contract_transition')
                result, canonical_name, canonical_bytes = prepare(state)
                if canonical_name != 'requirement-lineage.json' or not isinstance(canonical_bytes, bytes):
                    raise ValueError('transition prepare must return canonical lineage bytes')
                validate_ledger(manager.root, state)
            except (ValueError, KeyError, PermissionError) as error:
                task = original['tasks'].get(task_id)
                if task:
                    manager._event(original, 'task_action_rejected', task,
                                   observed={'action': 'approved_contract_transition', 'reason': str(error)})
                manager._save(original)
                manager._flush(original)
                raise
            canonical = _target(manager.root, canonical_name)
            # Match Tasks._save's redaction before creating the durable image.
            from harness.logging import redact
            ledger_bytes = (json.dumps(redact(state), ensure_ascii=False, indent=2) + '\n').encode()
            base = _base(manager.root)
            integration._mkdir(base)
            folder = base / ('txn-' + uuid.uuid4().hex)
            folder.mkdir(mode=0o700)
            integration._fsync_dir(base)
            record = {'schema_version': 1, 'transaction_id': folder.name,
                      'state': 'STAGING', 'task_id': task_id, 'files': []}
            integration._journal(folder, record)
            for index, (path, after, before) in enumerate(((canonical, canonical_bytes, before_canonical),
                                                         (ledger, ledger_bytes, before_ledger))):
                if path.read_bytes() != before:
                    raise ValueError('contract transition input changed during preparation')
                row = {'path': path.relative_to(manager.root).as_posix(),
                       'before_sha256': _sha(before), 'after_sha256': _sha(after),
                       'mode': stat.S_IMODE(path.stat().st_mode)}
                integration._write(folder / f'{index}-before.bin', before, 0o400)
                integration._write(folder / f'{index}-after.bin', after, 0o400)
                record['files'].append(row)
            record['state'] = 'PREPARED'
            integration._journal(folder, record)
            if fault_hook:
                fault_hook('prepared', None)
            record['state'] = 'APPLYING'
            integration._journal(folder, record)
            for row in record['files']:
                path = _target(manager.root, row['path'])
                if _sha(path.read_bytes()) != row['before_sha256']:
                    raise ValueError('contract transition target changed during publication')
                integration._write(path, _snapshot(folder, row, 'after'), row['mode'])
                if fault_hook:
                    fault_hook('applied', row['path'])
            if any(_sha(_target(manager.root, row['path']).read_bytes()) != row['after_sha256']
                   for row in record['files']):
                raise ValueError('contract transition target changed before commit')
            record['state'] = 'COMMITTED'
            integration._journal(folder, record)
            manager._flush(state)
            return result


def recover(manager, transaction_id, reason):
    """Explicitly roll back an interrupted publication; never rewrite unknown edits."""
    from harness.tracking_io import state_lock
    if (not isinstance(transaction_id, str) or not transaction_id.startswith('txn-')
            or len(transaction_id) != 36 or any(c not in '0123456789abcdef' for c in transaction_id[4:])
            or not isinstance(reason, str) or not reason.strip()):
        raise ValueError('explicit transaction ID and recovery reason required')
    with integration._locked(manager.root, kind='contract_transition'):
        with state_lock(manager.root, manager.directory / 'state.lock', 'recover_contract_transition'):
            folder = _base(manager.root) / transaction_id
            if (folder.is_dir() and not folder.is_symlink()
                    and not list(folder.iterdir())):
                # Crash between mkdir and the first journal, before any write.
                record = {'schema_version': 1, 'transaction_id': transaction_id,
                          'state': 'STAGING', 'files': []}
            else:
                record = _read(folder)
            if record['state'] in TERMINAL:
                return copy.deepcopy(record)
            if record['state'] == 'STAGING':
                record.update(state='ROLLED_BACK', staging_only=True, recovery_reason=reason)
                integration._journal(folder, record)
                return copy.deepcopy(record)
            # Validate ALL targets and snapshots before restoring either file.
            before = {}
            for row in record['files']:
                path = _target(manager.root, row['path'])
                _snapshot(folder, row, 'after')
                before[row['path']] = _snapshot(folder, row, 'before')
                if _sha(path.read_bytes()) not in {row['before_sha256'], row['after_sha256']}:
                    raise ValueError('unknown edits prevent contract transition recovery')
            record.update(state='ROLLING_BACK', recovery_reason=reason)
            integration._journal(folder, record)
            for row in reversed(record['files']):
                path = _target(manager.root, row['path'])
                if _sha(path.read_bytes()) not in {row['before_sha256'], row['after_sha256']}:
                    raise ValueError('target changed during contract transition recovery')
                integration._write(path, before[row['path']], row['mode'])
            record['state'] = 'ROLLED_BACK'
            integration._journal(folder, record)
            return copy.deepcopy(record)
