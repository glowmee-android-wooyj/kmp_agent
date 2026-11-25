"""Explicit local recovery. No scheduler, approval inference or PASS restoration."""
import copy
import functools
import hashlib
import json
import os
from pathlib import Path
import uuid


def managed_child(function):
    @functools.wraps(function)
    def wrapped(self, parent, spec, *args, **kwargs):
        from harness.process_ownership import TaskLease
        with TaskLease(self.root, spec['task_id']):
            return function(self, parent, spec, *args, **kwargs)
    return wrapped


def _record(root, run):
    ref = run.get('process_owner') or {}
    path = Path(ref.get('receipt_path', '/'))
    if not path.is_absolute() or not path.resolve().is_relative_to(Path(root).resolve()/'.harness'):
        raise ValueError('managed owner receipt unavailable')
    record = json.loads(path.read_text())
    if record.get('token') != ref.get('token') or record.get('task_id') != run['task_id']:
        raise ValueError('owner receipt identity mismatch')
    if run['agent_run_id'] not in record.get('run_ids', [record.get('agent_run_id')]):
        raise ValueError('owner receipt execution mismatch')
    return record


def workspace_basis(manager, task, reference=None):
    from harness import logging
    policy = (reference or {}).get('scan_policy') if reference is not None else 'generated_pruned_v2'
    if policy not in {None, 'generated_pruned_v1', 'generated_pruned_v2'}:
        raise ValueError('unknown recovery scan policy')
    prune_generated = policy is not None
    scan_options = dict(prune_generated=prune_generated,
                        prune_root_build_caches=policy == 'generated_pruned_v2')
    files = logging.snapshot(manager.root, **scan_options)
    modes = {p:(manager.root/p).stat().st_mode & 0o777 for p in files}
    # Logging snapshots intentionally omit links. Recovery must also pin their
    # names and raw targets, including dangling and directory links, without
    # following them outside the workspace. Keep the same generated exclusions.
    links = {}
    from harness.workspace_scan import paths
    for path in paths(manager.root, **scan_options):
        if path.is_symlink():
            links[str(path.relative_to(manager.root))] = os.readlink(path)
    result = {'files':files, 'modes':modes, 'symlinks':links}
    if policy is not None:result['scan_policy'] = policy
    if task.get('development_tracking'):
        from harness.development_tracking import capture, baseline
        result['code'] = capture(manager, task)
        result['baseline'] = baseline(task)
    return result


def basis_identity(value):
    value = copy.deepcopy(value)
    if value.get('code'):
        value['code'] = {k:value['code'].get(k) for k in ('state_sha256','scope')}
    return value


def _completed_command(run, record):
    """A normal command return can transfer a clean lease, never a worker."""
    from harness.process_ownership import inspect_child
    proof = next((p for p in run.get('command_owner_completions', [])
                  if p.get('token') == record.get('token')), None)
    aborted = next((p for p in run.get('command_prelaunch_aborts', [])
                    if p.get('token') == record.get('token')), None)
    # An explicit bounded-preflight abort certifies no child was created. It
    # never fabricates an exit code or a successful command completion.
    clean_abort = bool(aborted and not record.get('children')
                       and aborted.get('process_spawned') is False)
    proof = proof or (aborted if clean_abort else None)
    return bool(proof and proof.get('task_id') == run['task_id']
                and proof.get('agent_run_id') == run['agent_run_id']
                and proof.get('attempt_id') == run.get('attempt_id')
                and proof.get('receipt_path') == record.get('receipt_path')
                and (type(proof.get('returncode')) is int or clean_abort)
                and record.get('closed') is True and record.get('released') is True
                and not record.get('spawn_pending')
                and all(inspect_child(child)['status'] == 'DEAD'
                        for child in record.get('children', [])))


def _resume_grant(manager, run):
    """An explicit resume decision authorizes one ownership transfer only."""
    decisions = run.get('decisions', [])
    count = len(decisions)
    if (not count or any(p.get('decision_count') == count for p in run.get('owner_resume_claims', []))
            or not manager._valid_artifact(run.get('checkpoint', {}))
            or not manager._valid_artifact(decisions[-1])):
        return None
    return {'decision_count': count, 'checkpoint': copy.deepcopy(run['checkpoint']),
            'decision': copy.deepcopy(decisions[-1])}


def claim(manager, task_id, agent_run_id, *, allow_completed_command=False):
    from harness.process_ownership import current_lease
    lease = current_lease(manager.root, task_id)
    if lease is None:
        raise ValueError('managed process requires an exclusive task lease')
    def op(state):
        task = state['tasks'][task_id]
        run = next(r for r in task['executions'] if r['agent_run_id']==agent_run_id)
        if run.get('ended_at'):
            raise ValueError('cannot claim ended execution')
        previous = run.get('process_owner')
        new_owner = not previous or previous.get('token') != lease.record()['token']
        grant = _resume_grant(manager, run) if new_owner else None
        if previous and new_owner:
            prior = _record(manager.root, run)
            resumed = prior.get('released') and grant is not None
            if not (resumed or (allow_completed_command and _completed_command(run, prior))):
                raise ValueError('unfinished managed execution requires explicit recovery')
            run.setdefault('process_owner_history', []).append(copy.deepcopy(previous))
        if grant:
            grant.update(previous_token=(previous or {}).get('token'), token=lease.record()['token'])
            run.setdefault('owner_resume_claims', []).append(grant)
        lease.bind(agent_run_id)
        run['process_owner'] = lease.record()
        if 'recovery_start_basis' not in run:
            run['recovery_start_basis'] = workspace_basis(manager, task)
        return copy.deepcopy(run['process_owner'])
    return manager._mutate('claim_managed_owner', op, task_id)


def owned_worker(function):
    @functools.wraps(function)
    def wrapped(manager, task_id, agent_run_id, *args, **kwargs):
        from harness.process_ownership import TaskLease, current_lease
        if current_lease(manager.root, task_id):
            claim(manager, task_id, agent_run_id)
            return function(manager, task_id, agent_run_id, *args, **kwargs)
        with TaskLease(manager.root, task_id):
            claim(manager, task_id, agent_run_id)
            return function(manager, task_id, agent_run_id, *args, **kwargs)
    return wrapped


def _prelaunch_abort(manager, task_id, agent_run_id, error):
    """Save only a no-child proof; never replay timed-out authorization checks."""
    from harness.process_ownership import current_lease
    from harness.command_preflight import Preflight, current, stage
    from harness.tracking_io import state_lock
    from harness.integration_transaction import read_barrier
    from harness.protected_verification import validate_ledger
    budget = current()
    lease = current_lease(manager.root, task_id)
    try:
        with Preflight(manager.root, 2) as cleanup, stage('abort_ownership_cleanup'):
            with read_barrier(manager.root, timeout=2), state_lock(
                    manager.root, manager.directory/'state.lock', 'abort_command_prelaunch'):
                state = json.loads(manager.path.read_text())
                validate_ledger(manager.root, state)
                receipt = lease.record()
                if receipt.get('children') or receipt.get('spawn_pending'):
                    return  # Uncertain spawn must use recovery, never retry.
                task = state['tasks'][task_id]
                run = next(r for r in task['executions'] if r['agent_run_id'] == agent_run_id)
                if (run.get('ended_at') or (run.get('process_owner') or {}).get('token') != receipt['token']):
                    return
                proof = {'task_id':task_id, 'agent_run_id':agent_run_id,
                         'attempt_id':run.get('attempt_id'), 'token':receipt['token'],
                         'receipt_path':receipt['receipt_path'], 'process_spawned':False,
                         'error_type':type(error).__name__}
                run.setdefault('command_prelaunch_aborts', []).append(proof)
                manager._event(state, 'command_prelaunch_aborted', task, agent_run_id, observed=proof)
                validate_ledger(manager.root, state)
                # Durable outbox is flushed by the next ordinary operation.
                # Do not block timeout reporting on the full event-log scan.
                manager._save(state)
    except Exception as cleanup_error:
        # Missing proof keeps the existing strict recovery requirement. Preserve
        # the original timeout and do not infer clean ownership from a failure.
        if budget is not None:
            budget.emit('CLEANUP_DEFERRED', error_type=type(cleanup_error).__name__)


def owned_command(function):
    @functools.wraps(function)
    def wrapped(manager, task_id, *args, **kwargs):
        from harness.process_ownership import TaskLease, current_lease
        from harness.command_preflight import stage, PreflightTimeout
        with stage('ownership_dispatch'):
            aid = manager.require_dispatch(task_id, active_execution=True)
        if current_lease(manager.root,task_id):
            with stage('ownership_claim'):
                claim(manager,task_id,aid)
            return function(manager,task_id,*args,**kwargs)
        with TaskLease(manager.root,task_id):
            try:
                with stage('ownership_claim'):
                    claim(manager,task_id,aid,allow_completed_command=True)
                result = function(manager,task_id,*args,**kwargs)
            except PreflightTimeout as error:
                _prelaunch_abort(manager,task_id,aid,error)
                raise
            # Only a standalone managed command's normal return certifies this
            # boundary. Nested worker leases and thrown exceptions remain strict.
            lease = current_lease(manager.root, task_id)
            receipt = lease.record()
            def completed(state):
                live = state['tasks'][task_id]
                run = next(r for r in live['executions'] if r['agent_run_id'] == aid)
                if run.get('ended_at') or run.get('process_owner', {}).get('token') != receipt['token']:
                    raise ValueError('command ownership changed before completion')
                if type(getattr(result, 'returncode', None)) is not int:
                    raise ValueError('managed command completion requires an actual exit code')
                proof = {'task_id': task_id, 'agent_run_id': aid,
                         'attempt_id': run.get('attempt_id'), 'token': receipt['token'],
                         'receipt_path': receipt['receipt_path'], 'returncode': result.returncode}
                run.setdefault('command_owner_completions', []).append(proof)
                manager._event(state, 'managed_command_owner_completed', live, aid, observed=proof)
            manager._mutate('complete_command_owner', completed, task_id)
            return result
    return wrapped


def cancel_spawn(manager, task_id, intent):
    from harness.process_ownership import current_lease
    current_lease(manager.root,task_id).cancel_intent(intent)


def spawn_intent(manager, task_id, agent_run_id):
    from harness.process_ownership import current_lease
    claim(manager, task_id, agent_run_id)
    return current_lease(manager.root, task_id).spawn_intent()


def register_process(manager, task_id, child, intent):
    from harness.process_ownership import current_lease
    lease = current_lease(manager.root, task_id)
    try:
        lease.register_child(child, intent)
    except BaseException:
        # Registration uncertainty never leaves the process launched by this
        # caller running silently. Its immediate Popen handle is still owned;
        # historical/PID-only recovery never uses this path.
        if type(child).__module__ == 'subprocess' and getattr(child, 'poll', lambda:None)() is None:
            from harness.execution_policy import stop_owned_group
            stop_owned_group(child, lambda process, timeout:process.communicate(timeout=timeout))
        raise
    if not lease.record().get('guardian'):
        lease.start_guardian()


def progress_gate(manager, task_id, action):
    if action not in {'ready','begin','begin_review','submit','review','resume','dispatch','dispatch_preflight','accept','handoff'}:
        return
    from harness.integration_transaction import unresolved
    if unresolved(manager.root):
        raise ValueError('unresolved integration transaction; explicit recovery required')


def done_reasons(manager, task):
    from harness.verification import current_reasons
    review = (task.get('reviews') or [{}])[-1]
    reasons = current_reasons(manager, task, review)
    if not review:
        reasons.append('completion review missing')
    if task.get('validation_status', {}).get(review.get('agent_run_id'), {}).get('state') == 'RECHECK_REQUIRED':
        reasons.append('recorded validation requires recheck')
    if task.get('development_tracking'):
        from harness.development_tracking import capture, baseline
        from harness.completion import execution_code_matches
        try:
            current = capture(manager, task)
            proof = review.get('development_evidence', {})
            if (current['state_sha256'] != proof.get('code_state', {}).get('state_sha256')
                    and not execution_code_matches(task, review, current)) or baseline(task) != proof.get('baseline'):
                reasons.append('code or baseline changed')
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append('current code scope unknown')
    if task.get('qa_config'):
        from harness.qa import scoped_manifest
        config = task['qa_config']
        if ((config.get('attempts') or [{}])[-1].get('outcome') != 'PASS'
                or scoped_manifest(manager.root, config.get('manifest_scope', {'mode':'UNKNOWN'}), config.get('generated_paths', [])) != config.get('target_manifest')):
            reasons.append('QA observation stale or latest attempt not PASS')
    from harness.integration_transaction import unresolved
    if unresolved(manager.root):
        reasons.append('unresolved integration transaction')
    return sorted(set(reasons))


def inspect_execution(manager, task, run):
    from harness.process_ownership import inspect_owner, inspect_child, inspect_lease
    try:
        slot = inspect_lease(manager.root, task['task_id'])
        if slot.get('held'):
            return {'state':'ACTIVE', 'reason':'kernel task lease held; recovery forbidden'}
        record = _record(manager.root, run)
        owner = inspect_owner(record['owner'])
        if owner['status'] == 'ALIVE':
            return {'state':'ACTIVE', 'reason':'exact owner still alive'}
        if owner['status'] != 'DEAD' or record.get('spawn_pending'):
            return {'state':'MANUAL_INTERVENTION_REQUIRED', 'reason':'owner/spawn identity uncertain'}
        children = [inspect_child(c) for c in record.get('children', [])]
        if any(c['status'] != 'DEAD' for c in children):
            return {'state':'MANUAL_INTERVENTION_REQUIRED', 'reason':'registered child process group unresolved', 'children':children}
        return {'state':'INTERRUPTED', 'reason':'exact owner dead, lease free, registered children stopped', 'token':record['token']}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state':'MANUAL_INTERVENTION_REQUIRED', 'reason':'managed ownership evidence unavailable'}


def _safe_basis(manager, state, task, run):
    if not all(manager._valid_artifact(a) and (manager.root/a['path']).is_file()
               and hashlib.sha256((manager.root/a['path']).read_bytes()).hexdigest()==a['sha256'] for a in task.get('inputs', [])):
        return None, 'input bytes or snapshots changed'
    before = run.get('recovery_start_basis')
    if not before:
        return None, 'start workspace basis unavailable'
    current = workspace_basis(manager, task, reference=before)
    expected = copy.deepcopy(before)
    if task.get('development_delegation'):
        for child in task['development_delegation']['children']:
            completed = state['tasks'][child['task_id']]
            if completed['state'] == 'DONE' and not done_reasons(manager, completed):
                for name in completed.get('changed_files', []):
                    if name in current['files']:
                        expected['files'][name] = current['files'][name]
                        expected['modes'][name] = current['modes'][name]
                    else:
                        expected['files'].pop(name, None); expected['modes'].pop(name, None)
        # Reconstruct only already-reviewed child changes, never an unreviewed
        # partial integration. HEAD/index/declared baseline remain exact.
        if expected.get('code') and current.get('code'):
            old, new = expected['code'], current['code']
            if any(old.get(k)!=new.get(k) for k in ('head','index','scope')) or expected.get('baseline')!=current.get('baseline'):
                return None, 'coordinator Git/baseline changed'
            expected['code'] = copy.deepcopy(current['code'])
    if basis_identity(expected) != basis_identity(current):
        return None, 'workspace changed outside verified completed results'
    return current, None


def recover_tasks(manager, task_ids=None):
    from harness.process_ownership import exclusive_slot
    from harness.integration_transaction import recover as recover_integration, unresolved
    integrations = recover_integration(manager.root)
    results = []
    initial = manager.read()
    selected = task_ids or [t['task_id'] for t in initial['tasks'].values()
                            if any(r.get('run_id')==manager.run_id for r in t.get('executions', []))]
    for tid in selected:
        try:
            # No lock file age/PID-only inference. Recovery competes for the same
            # kernel slot used by adapters, then checks exact owner in state lock.
            with exclusive_slot(manager.root, tid):
                def op(state):
                    task = state['tasks'][tid]
                    active = [r for r in task['executions'] if not r.get('ended_at')]
                    if not active:
                        return {'task_id':tid, **copy.deepcopy(task.get('recovery', {'state':'RECHECK_REQUIRED','reason':'no interrupted active execution'}))}
                    run = active[0]
                    from harness.process_ownership import inspect_owner, inspect_child
                    try:
                        record = _record(manager.root, run)
                        owner = inspect_owner(record['owner'])
                        if owner['status']=='ALIVE':
                            return {'task_id':tid,'state':'ACTIVE','reason':'exact owner alive'}
                        if owner['status']!='DEAD' or record.get('spawn_pending') or any(inspect_child(c)['status']!='DEAD' for c in record.get('children', [])):
                            return {'task_id':tid,'state':'MANUAL_INTERVENTION_REQUIRED','reason':'ownership or orphan group unresolved'}
                    except (ValueError,OSError,KeyError,TypeError):
                        return {'task_id':tid,'state':'MANUAL_INTERVENTION_REQUIRED','reason':'owner receipt unavailable'}
                    current, reason = _safe_basis(manager, state, task, run)
                    if unresolved(manager.root):
                        current, reason = None, 'integration requires manual intervention'
                    if run['kind']!='work':
                        current, reason = None, 'interrupted review must be independently re-executed'
                    recovery_id = hashlib.sha256((tid+run['agent_run_id']+record['token']).encode()).hexdigest()
                    folder=manager.directory/'recovery'; folder.mkdir(exist_ok=True)
                    checkpoint=folder/(recovery_id+'.json')
                    payload={'version':1,'recovery_id':recovery_id,'task_id':tid,'interrupted_execution':run['agent_run_id'],
                             'current_basis':current,'reason':reason,'observed_at':manager.logger.execution_context(manager.run_id).get('observed_at','UNKNOWN'),
                             'prior_checkpoint':run.get('checkpoint'),'authority':'RECOVERY_CHECKPOINT_ONLY_NO_PASS'}
                    from harness.incremental_sync import atomic_json
                    atomic_json(checkpoint,payload)
                    receipt={'state':'RECOVERABLE' if current else 'RECHECK_REQUIRED','recovery_id':recovery_id,
                             'interrupted_execution':run['agent_run_id'],'reason':reason,
                             'checkpoint':manager.artifact(str(checkpoint.relative_to(manager.root))), 'automatic_execution':False}
                    from harness.logging import now
                    recovered_at=now()
                    run.update(ended_at=recovered_at, outcome='INTERRUPTED', recovery=copy.deepcopy(receipt),
                               end_time_semantics='RECOVERY_OBSERVATION_NOT_EXACT_PROCESS_DEATH')
                    for admission in run.get('runtime_admissions', []):
                        if not admission.get('released_at'):
                            admission.update(released_at=recovered_at, release_reason='CONFIRMED_OWNER_INTERRUPTION')
                    for admission in state.get('runtime_budgets', {}).get(run['run_id'], {}).get('admissions', []):
                        if admission.get('agent_run_id')==run['agent_run_id'] and not admission.get('released_at'):
                            admission.update(released_at=recovered_at, release_reason='CONFIRMED_OWNER_INTERRUPTION')
                    from harness.sequential import wait_end, finish
                    wait_end(manager, state, task, run, 'INTERRUPTED')
                    finish(manager, task, run)
                    task.update(state='BLOCKED',blocked_reason=reason or 'confirmed interruption; explicit safe resume required',recovery=receipt)
                    task.setdefault('recovery_history',[]).append(copy.deepcopy(receipt))
                    manager._event(state,'execution_interruption_recovered',task,run['agent_run_id'],observed=receipt)
                    return {'task_id':tid, **copy.deepcopy(receipt)}
                results.append(manager._mutate('recover_execution',op,tid))
        except (BlockingIOError, RuntimeError) as exc:
            results.append({'task_id':tid,'state':'ACTIVE','reason':str(exc)})
    return {'tasks':results,'integration_recovery':integrations,'automatic_execution':False}


def resume_task(manager, task_id, worker=None):
    from harness.process_ownership import TaskLease
    task = manager.read()['tasks'][task_id]
    if task['state']=='DONE':
        reasons=done_reasons(manager,task)
        return {'task_id':task_id,'cached':True,'state':'RECHECK_REQUIRED' if reasons else 'DONE','reasons':reasons,'official_completion_inferred':False}
    receipt=task.get('recovery') or {}
    if receipt.get('state')!='RECOVERABLE' or receipt.get('resume_dispatched'):
        raise ValueError('explicit recoverable checkpoint required; repeated admission is blocked')
    manager.require_dispatch(task_id, preflight=True)
    if not manager._valid_artifact(receipt['checkpoint']):
        raise ValueError('recovery checkpoint changed')
    saved=json.loads((manager.root/receipt['checkpoint']['snapshot']).read_text())
    if basis_identity(workspace_basis(manager,task,reference=saved['current_basis']))!=basis_identity(saved['current_basis']):
        raise ValueError('recovery basis changed; recheck required')
    with TaskLease(manager.root,task_id):
        def reserve(state):
            live=state['tasks'][task_id]
            if live.get('recovery')!=receipt or any(not r.get('ended_at') for r in live['executions']):
                raise ValueError('recovery admission changed or already active')
            if basis_identity(workspace_basis(manager,live,reference=saved['current_basis']))!=basis_identity(saved['current_basis']):
                raise ValueError('recovery workspace changed during admission')
            if len([r for r in live['executions'] if r['kind']=='work'])>=live['max_attempts']:
                raise ValueError('attempt budget exhausted')
            live['recovery']['resume_dispatched']=str(uuid.uuid4())
            manager._event(state,'recovery_resume_admitted',live,observed=copy.deepcopy(live['recovery']))
        manager._mutate('admit_recovery_resume',reserve,task_id)
        if task.get('development_delegation'):
            from harness.delegation import Development
            return Development(manager.root,manager.run_id).execute(task_id,worker=worker)
        if task.get('qa_config'):
            from harness.qa import QA
            return QA(manager.root,manager.run_id).execute(task_id,worker=worker)
        from harness.roles import Roles
        return Roles(manager.root,manager.run_id).execute(task_id,task['role'],worker=worker)
