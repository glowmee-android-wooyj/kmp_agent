"""Exact native-approved technical revisions; opening approvals remain immutable."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

from harness.decisions import _task_basis, digest


def normalized(basis):
    value = copy.deepcopy(basis)
    for row in value.values():
        if isinstance(row.get('code_basis'), dict):
            row['code_basis'].pop('state_sha256', None)
    return value


def records(root, state, task):
    """Authenticate transition metadata against content-addressed proof and audit event."""
    if not task.get('contract_transitions'):
        return []
    from harness.logging import Logger
    events = Logger(root).read()
    persisted_keys = {event.get('source_key') for event in events}
    events.extend(event for event in state.get('outbox', [])
                  if event.get('source_key') not in persisted_keys)
    result = []
    for link in task.get('contract_transitions', []):
        ref = link['proof']
        path = Path(root) / ref['snapshot']
        if (path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']):
            raise ValueError('contract transition proof missing or corrupt')
        proof = json.loads(path.read_text())
        if proof != {k:v for k,v in link.items() if k != 'proof'}:
            raise ValueError('contract transition proof differs from recorded transaction')
        event = [e for e in events if e.get('source_key') == 'contract-transition:' + proof['id']]
        if (len(event) != 1 or event[0].get('event_type') != 'approved_contract_transition'
                or event[0].get('task_id') != task['task_id']
                or event[0].get('agent_run_id') != proof['agent_run_id']
                or event[0].get('run_id') != proof['run_id']
                or event[0].get('observed') != link):
            raise ValueError('contract transition audit evidence missing or changed')
        if (proof['task_id'] != task['task_id'] or proof['root'] != str(Path(root).resolve())
                or state.get('execution_policy_bindings', {}).get(proof['run_id']) != proof['root_binding']):
            raise ValueError('contract transition execution binding changed')
        result.append(proof)
    return result


def approval_basis(root, state, q, expected):
    """Project only a verified exact question's approved input replacement."""
    result = copy.deepcopy(expected)
    for tid in q['affected_task_ids']:
        for row in records(root, state, state['tasks'][tid]):
            if row['question_id'] != q['id']:
                continue
            if (row['decision_contract_sha256'] != digest(q['decision_contract'])
                    or normalized({tid:result[tid]}) != normalized({tid:row['before_task_basis']})):
                raise ValueError('approval transition does not extend its original exact task basis')
            result[tid] = copy.deepcopy(row['after_task_basis'])
    return result


def effective_baseline(root, state, task, run):
    value = copy.deepcopy(run['baseline'])
    for row in records(root, state, task):
        if row['agent_run_id'] != run['agent_run_id']:
            continue
        if row['attempt_id'] != run['attempt_id'] or value != row['before_baseline']:
            raise ValueError('contract transition does not extend original execution baseline')
        value = copy.deepcopy(row['after_baseline'])
    return value


def _technical_only(before, after):
    """No unrelated requirement, original source or implementation scope rebinding."""
    from harness.development_constraints import KEY
    if before['scope'] != 'PRODUCT' or after['scope'] != 'PRODUCT':
        raise ValueError('approved contract transition requires PRODUCT lineage')
    old = [r for r in before['requirements'] if KEY in r['expected']]
    new = [r for r in after['requirements'] if KEY in r['expected']]
    if len(old) != 1 or len(new) != 1 or old[0]['id'] != new[0]['id']:
        raise ValueError('exactly one existing technical requirement may be revised')
    tech = old[0]['id']
    if ({k:v for k,v in old[0]['expected'].items() if k != KEY}
            != {k:v for k,v in new[0]['expected'].items() if k != KEY}):
        raise ValueError('technical requirement cannot alter unrelated expected properties')
    for rows in ('requirements', 'derived_items'):
        def unrelated(c):
            return [r for r in c[rows] if (r['id'] != tech if rows == 'requirements' else r['source_ids'] != [tech])]
        if unrelated(before) != unrelated(after):
            raise ValueError('contract transition cannot alter nontechnical requirements or derivations')
    # Every source used by an unrelated requirement/derivation stays byte-identical.
    protected = set()
    for c in (before, after):
        protected.update(r['source'] for r in c['requirements'] if r['id'] != tech)
        protected.update(r['source'] for r in c['derived_items'] if r['source_ids'] != [tech])
        protected.update(s['id'] for s in c['sources'] if s['kind'] == 'USER_ORIGINAL')
    technical_sources = {r['source'] for c in (before, after) for r in c['requirements'] if r['id'] == tech}
    technical_sources.update(r['source'] for c in (before, after) for r in c['derived_items'] if r['source_ids'] == [tech])
    old_sources = {s['id']:s for s in before['sources']}
    new_sources = {s['id']:s for s in after['sources']}
    if any(old_sources.get(sid) != new_sources.get(sid)
           for sid in (set(old_sources) | set(new_sources)) - technical_sources):
        raise ValueError('contract transition cannot alter unrelated or unused sources')
    old_derived = {r['id']:r for r in before['derived_items'] if r['source_ids'] == [tech]}
    new_derived = {r['id']:r for r in after['derived_items'] if r['source_ids'] == [tech]}
    for did in set(old_derived) | set(new_derived):
        a = {k:v for k,v in old_derived.get(did, {}).get('expected', {}).items() if k != KEY}
        b = {k:v for k,v in new_derived.get(did, {}).get('expected', {}).items() if k != KEY}
        if a != b:
            raise ValueError('technical derivation cannot change unrelated expectations')
    for sid in protected:
        a = next((s for s in before['sources'] if s['id'] == sid), None)
        b = next((s for s in after['sources'] if s['id'] == sid), None)
        if a != b:
            raise ValueError('contract transition cannot alter original or nontechnical sources')
    if before.get('decisions', []) != after.get('decisions', []):
        raise ValueError('contract transition cannot rewrite prior requirement decisions')
    return new[0]


def transition(manager, task_id, agent_run_id, question_id, candidate, before_sha256, reason, evidence):
    from harness.contract_transition_transaction import run as transact
    from harness.requirement_lineage import NAME, load, compare, approval_subject
    from harness.development_constraints import KEY, validate, _settings, _digest
    from harness.development_tracking import baseline
    from harness.approvals import human_approval
    from harness.process_ownership import exclusive_slot, inspect_child
    from harness.recovery import _record

    if not reason or not evidence:
        raise ValueError('contract transition requires reason and evidence')
    with exclusive_slot(manager.root, task_id):
        def prepare(state):
            task = state['tasks'][task_id]
            execution = manager._active(task, agent_run_id, 'work')
            if (task['state'] != 'BLOCKED' or execution['run_id'] != manager.run_id
                    or not manager._valid_artifact(execution.get('checkpoint', {}))):
                raise ValueError('contract transition requires original checkpointed BLOCKED work attempt')
            if execution.get('process_owner'):
                owner = _record(manager.root, execution)
                if (not owner.get('closed') or not owner.get('released') or owner.get('spawn_pending')
                        or any(inspect_child(c)['status'] != 'DEAD' for c in owner.get('children', []))):
                    raise ValueError('contract transition requires settled executor ownership')
            binding = state.get('execution_policy_bindings', {}).get(manager.run_id)
            if not binding:
                raise ValueError('contract transition requires original native root binding')
            q = state['required_questions'][question_id]
            if q['run_id'] != manager.run_id or task_id not in q['affected_task_ids']:
                raise ValueError('approved question must belong to original run and target task')
            before_bytes = (manager.root / NAME).read_bytes()
            if hashlib.sha256(before_bytes).hexdigest() != before_sha256:
                raise ValueError('canonical contract changed before transition')
            old = load(manager.root)
            path = manager.root / candidate
            if (Path(candidate).is_absolute() or '..' in Path(candidate).parts or path.is_symlink()
                    or not path.resolve().is_relative_to(manager.root)):
                raise ValueError('candidate must be a safe project-relative file')
            raw = path.read_bytes()
            proposed = json.loads(raw)
            # Accept an explicit handoff envelope as well as a plain lineage document.
            after = proposed['after'] if proposed.get('target_path') == NAME else proposed
            after = load(manager.root, copy.deepcopy(after), state=state)
            row = _technical_only(old, after)
            policy = validate(row['expected'][KEY])
            _settings(manager, task, policy)
            auth = after.get('human_authorization') or {}
            artifacts = {s['path']:s['sha256'] for s in after['sources']}
            receipt = human_approval(manager.root, state, question_id, task_id,
                                     'requirement_lineage', approval_subject(after), artifacts)
            if (not receipt or receipt.get('question_id') != question_id
                    or auth.get('question_id') != question_id
                    or auth.get('task_id') not in receipt['task_ids']):
                raise ValueError('exact proposed contract requires existing current native approval')
            comparison = compare(manager.root, after, state)
            if not comparison['structurally_ready'] or not comparison['authorization_ready']:
                raise ValueError('approved candidate lineage must be complete and structurally ready')
            frozen = task.get('stage_contract', {}).get(KEY)
            if frozen and frozen['policy'] == policy:
                raise ValueError('contract transition requires a changed approved technical policy')
            if not frozen or frozen['lineage']['sha256'] != before_sha256:
                raise ValueError('task must freeze the exact current technical contract')
            oldrefs = [frozen['lineage']] + frozen['sources']
            for ref in oldrefs:
                if ref not in task['inputs'] or not manager._valid_artifact(ref):
                    raise ValueError('frozen technical inputs missing or corrupt')
                source = manager.root / ref['path']
                if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != ref['sha256']:
                    raise ValueError('existing technical input changed before transition')
            before_basis = _task_basis(state, manager.root, [task_id], include_code=True)[task_id]
            before_baseline = baseline(task)
            # Canonical bytes are generated from the exact approved subject and its locator.
            canonical = (json.dumps(after, ensure_ascii=False, indent=2) + '\n').encode()
            canonical_ref = _artifact_bytes(manager, NAME, canonical)
            sources = [manager.artifact(s['path']) for s in after['sources']]
            old_by_path = {r['path']:r for r in oldrefs}
            sources = [copy.deepcopy(old_by_path[r['path']]) if r['path'] in old_by_path
                       and old_by_path[r['path']]['sha256'] == r['sha256'] else r for r in sources]
            newrefs = [canonical_ref] + sources
            oldpaths = {ref['path'] for ref in oldrefs}
            newpaths = {ref['path'] for ref in newrefs}
            if any(ref['path'] in newpaths-oldpaths for ref in task['inputs']):
                raise ValueError('new technical source collides with unrelated frozen input')
            task['inputs'] = [ref for ref in task['inputs'] if ref['path'] not in oldpaths] + copy.deepcopy(newrefs)
            task['stage_contract'][KEY] = {'policy':policy, 'sha256':_digest(policy),
                'requirement_id':row['id'], 'lineage':canonical_ref, 'sources':sources}
            # Tracking P/D/API/policy authority is preserved; technical sources cannot replace it.
            if any(ref['path'] in oldpaths and ref not in newrefs
                   for ref in task['development_tracking']['baselines'].values()):
                raise ValueError('technical transition cannot replace planning/design/API/policy baselines')
            after_basis = copy.deepcopy(before_basis)
            after_basis['inputs'] = copy.deepcopy(task['inputs'])
            after_basis['input_bytes'] = {ref['path']:ref['sha256'] for ref in task['inputs']}
            proof = {'version':1, 'id':str(uuid.uuid4()), 'root':str(manager.root),
                'run_id':manager.run_id, 'root_binding':copy.deepcopy(binding), 'task_id':task_id,
                'agent_run_id':agent_run_id, 'attempt_id':execution['attempt_id'],
                'question_id':question_id, 'decision_contract_sha256':digest(q['decision_contract']),
                'before_task_basis':before_basis, 'after_task_basis':after_basis,
                'before_baseline':before_baseline, 'after_baseline':baseline(task),
                'before_sha256':before_sha256, 'after_sha256':hashlib.sha256(canonical).hexdigest(),
                'reason':reason, 'evidence':manager.artifact(evidence),
                'revalidation':'REQUIRED; prior QA and review records remain historical'}
            ref = _artifact_bytes(manager, '.harness/contract-transition-proof.json',
                                  (json.dumps(proof, ensure_ascii=False, sort_keys=True)+'\n').encode())
            link = dict(proof, proof=ref)
            task.setdefault('contract_transitions', []).append(link)
            for review in task.get('reviews', []):
                if review.get('development_evidence'):
                    status = {'state':'RECHECK_REQUIRED', 'reasons':['approved technical contract transition']}
                    task.setdefault('validation_status', {})[review['agent_run_id']] = status
                    manager._event(state, 'development_validation_invalidated', task,
                                   review['agent_run_id'], observed=status)
            manager._event(state, 'approved_contract_transition', task, agent_run_id, observed=link)
            state['outbox'][-1]['source_key'] = 'contract-transition:' + proof['id']
            return copy.deepcopy(link), NAME, canonical
        return transact(manager, task_id, prepare)


def _artifact_bytes(manager, name, data):
    """Freeze exact bytes without writing or temporarily replacing a canonical input."""
    import fcntl
    import os
    from harness.logging import digest as byte_digest
    sha = byte_digest(data)
    folder = manager.root / '.harness/artifacts'
    if (any(p.is_symlink() for p in (manager.root/'.harness', folder))
            or not folder.resolve().is_relative_to(manager.root)):
        raise ValueError('contract transition artifact folder cannot be symlink')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (sha + '.txt')
    if folder.is_symlink():
        raise ValueError('contract transition artifact folder cannot be symlink')
    if (folder/'snapshot.lock').is_symlink():
        raise ValueError('contract transition snapshot lock cannot be symlink')
    with (folder/'snapshot.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            with path.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            from harness.integration_transaction import _fsync_dir
            _fsync_dir(folder)
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError('contract transition artifact collision')
    return {'path':name, 'sha256':sha, 'snapshot':str(path.relative_to(manager.root)),
            'snapshot_sha256':sha, 'git_commit':'UNKNOWN'}
