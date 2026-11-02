"""Durable single-worker scheduler. Only orchestrator decisions accept work."""
from __future__ import annotations
import contextlib
import hashlib
import json
import queue
import threading
import time
import traceback
from pathlib import Path
from models import validate_tasks
from store import Store
from worker import Runner
from gitops import clean_base,create_worktree,capture_patch,apply_patch,run_git
from engine_support import StateLock,artifact_text,context_text,pid_alive,redact

TERMINAL={'cancelled','completed'}
class Engine:
    def __init__(self,settings,runner=None,on_event=None):
        self.settings=settings; self.on_event=on_event; self.owner=StateLock(settings.state_dir)
        try:
            self.store=Store(self.owner.directory); self.store.recover()
            self.lock=threading.RLock(); self.jobs=queue.Queue(); self.cancel_events={}; self.current=None; self.closed=False; self.probed=False
            self.runner=runner or Runner(settings,on_process=self._process_changed)
            if runner is not None and hasattr(runner,'on_process'): runner.on_process=self._process_changed
            self.thread=threading.Thread(target=self._loop,daemon=True); self.thread.start()
        except BaseException:
            self.owner.close(); raise
    def root(self,bid): return self.owner.directory/'runs'/bid
    def _process_changed(self,pid):
        with self.lock:
            if self.current: self.store.update(self.current,active_pid=pid)
    def _update(self,bid,status=None,**fields):
        with self.lock:
            previous=self.store.get(bid)['status']
            if previous in TERMINAL: return False
            self.store.update(bid,status,**fields)
            if self.on_event and status in {'needs_review','final_review','blocked'} and status!=previous:
                with contextlib.suppress(Exception): self.on_event(dict(batch_id=bid,status=status,error=redact(fields.get('error') or '')[:300]))
            return True
    def _artifact(self,bid,path):
        path=Path(path).resolve(); root=self.root(bid).resolve()
        if not path.is_relative_to(root): raise ValueError('Artifact registration escapes batch')
        identity=path.relative_to(root).as_posix()
        with self.lock:
            data=self.store.get(bid); mapping=data.get('artifacts',{}); mapping[identity]=str(path)
            manifest=root/'manifest.json'; mapping['manifest']=str(manifest)
            manifest.write_text(json.dumps({'artifacts':list(mapping)},indent=2),encoding='utf-8')
            self.store.update(bid,artifacts=mapping)
        return identity
    def _write(self,bid,path,content):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        if isinstance(content,bytes): path.write_bytes(content)
        else: path.write_text(content,encoding='utf-8')
        return self._artifact(bid,path)
    def _register_run(self,bid,result,directory):
        ids=[]
        if result.get('full_response_path'):
            result['full_response_artifact']=self._artifact(bid,result['full_response_path'])
            ids.append(result['full_response_artifact'])
        for path in result.get('log_paths',[]):
            if Path(path).is_file(): ids.append(self._artifact(bid,path))
        if (Path(directory)/'result.json').is_file(): ids.append(self._artifact(bid,Path(directory)/'result.json'))
        return ids
    def submit_batch(self,repo_path,tasks):
        parsed=validate_tasks(tasks); root,base=clean_base(Path(repo_path))
        with self.lock:
            if self.closed: raise ValueError('Bridge is closing')
            bid=self.store.create(dict(repo_path=str(root),base=base,model=self.settings.model,tasks=[t.model_dump() for t in parsed],results=[],artifacts={},reads={},active_pid=None,cloud_tokens=None,error=None))
            self.root(bid).mkdir(parents=True)
            self.cancel_events[bid]=threading.Event(); self.store.event(bid,'submitted',actor='orchestrator',task_ids=[t.id for t in parsed]); self.jobs.put((bid,'tasks'))
            return dict(batch_id=bid,status='queued',task_ids=[t.id for t in parsed])
    def get_batch(self,batch_id):
        with self.lock:
            batch=self.store.get(batch_id); self.store.event(batch_id,'status_read',actor='orchestrator')
            results=[]
            for task in batch.get('results',[]):
                results.append(dict(id=task['id'],status=task.get('status'),attempts=len(task.get('attempts',[])),summary=redact(task.get('summary',''))[:400],required_artifacts=task.get('required_artifacts',[]),checks=[dict(exit_code=c.get('exit_code'),success=c.get('success')) for c in task.get('checks',[])],criteria='pending_orchestrator_review' if batch['status']!='completed' else 'accepted_by_orchestrator'))
            integration=batch.get('integration') or {}
            metrics=dict(local_seconds=round(sum(a.get('elapsed_seconds',0) for t in batch.get('results',[]) for a in t.get('attempts',[]))+integration.get('local_seconds',0),3),tool_calls=sum(a.get('tool_count',0) for t in batch.get('results',[]) for a in t.get('attempts',[]))+integration.get('tool_calls',0),repairs=sum(max(0,len(t.get('attempts',[]))-1) for t in batch.get('results',[])),cloud_tokens=None,orchestrator_interactions=sum(e.get('actor')=='orchestrator' for e in self.store.events(batch_id)))
            return dict(batch_id=batch_id,status=batch['status'],error=redact(batch.get('error') or '')[:1200],model=batch['model'],tasks=results,integration=batch.get('integration'),manifest='manifest' if batch.get('artifacts') else None,active_pid=batch.get('active_pid'),metrics=metrics)
    def read_artifact(self,batch_id,artifact_id,cursor=0):
        with self.lock:
            batch=self.store.get(batch_id)
            if artifact_id not in batch.get('artifacts',{}): raise ValueError('Unknown artifact ID')
            page=artifact_text(self.root(batch_id),batch['artifacts'][artifact_id],cursor)
            reads=batch.get('reads',{}); previous=reads.get(artifact_id,0)
            if cursor<=previous: reads[artifact_id]=max(previous,cursor+len(page['content']))
            self.store.update(batch_id,reads=reads); self.store.event(batch_id,'artifact_read',actor='orchestrator',artifact_id=artifact_id,cursor=cursor,end=cursor+len(page['content']))
            return dict(artifact_id=artifact_id,**page)
    def _require_reads(self,batch,ids):
        missing=[]
        for identity in ids:
            full=artifact_text(self.root(batch['id']),batch['artifacts'][identity],0)
            if batch.get('reads',{}).get(identity,0)<full['total_chars']: missing.append(identity)
        if missing: raise ValueError('Read all pages of required evidence before approval: '+', '.join(missing))
    def decide_batch(self,batch_id,action,feedback=''):
        if action not in {'approve','revise','stop'}: raise ValueError('Unknown decision')
        if action=='stop': return self.cancel_batch(batch_id)
        with self.lock:
            batch=self.store.get(batch_id); status=batch['status']
            if action=='approve':
                if status not in {'needs_review','final_review'}: raise ValueError('Approval requires needs_review or final_review')
                required=batch.get('integration',{}).get('required_artifacts',[]) if status=='final_review' else [a for t in batch['results'] for a in t.get('required_artifacts',[])]
                self._require_reads(batch,required)
                if status=='final_review' and not batch['integration'].get('checks_passed'): raise ValueError('Integration checks failed')
                self.store.event(batch_id,'final_accepted' if status=='final_review' else 'review_approved',actor='orchestrator',feedback=feedback)
                if status=='final_review' or all(t['role']=='explore' for t in batch['tasks']):
                    for task in batch['results']: task['status']='accepted'
                    self.store.update(batch_id,'completed',results=batch['results'],accepted_at=time.time(),error=None)
                else:
                    self.store.update(batch_id,'integrating',error=None); self.jobs.put((batch_id,'integrate'))
            else:
                if status not in {'needs_review','blocked','final_review'}: raise ValueError('Revision requires a reviewable or blocked batch')
                if not feedback.strip(): raise ValueError('Revision requires concrete feedback')
                if pid_alive(batch.get('active_pid')): raise ValueError('Previous process is still active; cancel/inspect it before resuming')
                if any(len(t.get('attempts',[]))>=self.settings.max_attempts for t in batch['results']): raise ValueError('Task attempt budget exhausted; submit a new batch')
                self.cancel_events[batch_id]=threading.Event()
                self.store.event(batch_id,'revision_requested',actor='orchestrator',feedback=feedback)
                for task in batch['results']: task['status']='pending'
                self.store.update(batch_id,'queued',feedback=feedback,results=batch['results'],error=None,integration=None)
                self.jobs.put((batch_id,'tasks'))
            return dict(batch_id=batch_id,status=self.store.get(batch_id)['status'])
    def cancel_batch(self,batch_id):
        with self.lock:
            batch=self.store.get(batch_id)
            if batch['status'] in TERMINAL: return dict(batch_id=batch_id,status=batch['status'])
            self.cancel_events.setdefault(batch_id,threading.Event()).set()
            self.store.event(batch_id,'cancelled',actor='orchestrator'); self.store.update(batch_id,'cancelled',error='Cancelled by orchestrator')
            return dict(batch_id=batch_id,status='cancelled')
    def _loop(self):
        while True:
            job=self.jobs.get()
            if job is None: return
            bid,operation=job
            with self.lock:
                if self.closed: return
                batch=self.store.get(bid)
                if batch['status'] in TERMINAL: continue
                self.current=bid
            try:
                if not self.probed:
                    self.runner.preflight(probe=True); self.probed=True
                if operation=='integrate': self._integrate(bid)
                else: self._execute(bid)
            except Exception as exc:
                self._write(bid,self.root(bid)/'error.txt',traceback.format_exc())
                self._update(bid,'blocked',error=str(exc))
                self.store.event(bid,'blocked',error=str(exc))
            finally:
                with self.lock:
                    self.store.update(bid,active_pid=None); self.current=None
    def close(self):
        with self.lock:
            if self.closed: return
            self.closed=True
            if self.current: self.cancel_events.setdefault(self.current,threading.Event()).set()
            self.jobs.put(None)
        self.thread.join(timeout=20)
        if self.thread.is_alive(): raise RuntimeError('Worker still stopping; state lock retained')
        self.store.recover(); self.owner.close()
    def _execute(self,bid):
        if not self._update(bid,'running',error=None): return
        batch=self.store.get(bid); cancel=self.cancel_events.setdefault(bid,threading.Event())
        results=batch.get('results',[])
        for task in batch['tasks']:
            if cancel.is_set(): return
            entry=next((r for r in results if r['id']==task['id']),None)
            if entry is None:
                entry=dict(id=task['id'],status='pending',attempts=[],required_artifacts=[]); results.append(entry)
            if entry['status']=='needs_review': continue
            worktree=self.root(bid)/'tasks'/task['id']/'worktree'
            if not worktree.exists(): create_worktree(Path(batch['repo_path']),worktree,batch['base'])
            entry['workspace']=str(worktree)
            feedback=batch.get('feedback','')
            while len(entry['attempts'])<self.settings.max_attempts:
                number=len(entry['attempts'])+1; directory=self.root(bid)/'tasks'/task['id']/f'attempt-{number}'
                directory.mkdir(parents=True,exist_ok=True); started=time.monotonic()
                attempt=dict(number=number,model=self.settings.model,tool_count=0,elapsed_seconds=0,status='running')
                entry['attempts'].append(attempt); entry['status']='running'
                if not self._update(bid,results=results): return
                prompt=('APPROVED TASK\n'+json.dumps(task,indent=2)+'\nORCHESTRATOR/REPAIR FEEDBACK\n'+feedback+
                        '\nUse native file tools only. Do not run tests or self-review. The bridge runs verification. '
                        'Implement only allowed files; stop immediately after edits with a short result. '
                        'For exploration, read only and report concrete findings; never edit.')
                run_dir=directory/'worker'
                result=self.runner.run(worktree,prompt,run_dir,cancel,readonly=task['role']=='explore',allowed_files=task['allowed_files'],budget_tools=self.settings.max_tools,time_budget=self.settings.timeout_seconds)
                attempt.update(session_id=result.get('session_id'),tool_count=result.get('tool_count',0),usage=result.get('usage'),error=result.get('error'))
                self._register_run(bid,result,run_dir)
                if (result.get('error') or '').startswith('Local model unavailable'):
                    attempt.update(status='failed',elapsed_seconds=round(time.monotonic()-started,3))
                    self._update(bid,results=results)
                    raise RuntimeError(result['error'])
                patch=capture_patch(worktree,task['allowed_files'])
                patch_id=self._write(bid,directory/'diff.patch',patch)
                context_id=self._write(bid,directory/'context.txt',context_text(worktree,task['allowed_files']))
                remaining=lambda:self.settings.timeout_seconds-(time.monotonic()-started)
                checks=self.runner.verify(worktree,task['verification_commands'],directory/'verification',cancel,time_budget=max(0,remaining())) if result['success'] else []
                for check in checks:
                    for key in ['stdout','stderr']:
                        if check.get(key) and Path(check[key]).is_file(): self._artifact(bid,check[key])
                checks_id=self._write(bid,directory/'checks.json',json.dumps(checks,indent=2))
                # Verification commands may generate tracked or unapproved files, too.
                if capture_patch(worktree,task['allowed_files'])!=patch: raise ValueError('Verification mutated reviewed project files')
                okay=result['success'] and len(checks)==len(task['verification_commands']) and all(c['success'] for c in checks)
                report=result
                if okay and task['role']=='implement':
                    budget=self.settings.max_tools-attempt['tool_count']
                    if budget<=0 or remaining()<=0: okay=False; attempt['error']='Attempt budget exhausted before review'
                    else:
                        review_dir=directory/'review'
                        source=context_text(worktree,task['allowed_files'])
                        if len(source)>32000: raise ValueError('Split this task: captured source exceeds the local review context budget')
                        report=self.runner.run(worktree,'READ ONLY REVIEW. Assess this captured source against the original criteria. Return findings, then STOP.\n'+json.dumps(task)+'\nCOMPLETE SOURCE\n'+source,review_dir,cancel,readonly=True,allowed_files=task['allowed_files'],budget_tools=budget,time_budget=remaining())
                        self._register_run(bid,report,review_dir)
                        attempt['tool_count']+=report.get('tool_count',0); attempt['review_session_id']=report.get('session_id')
                        attempt['review_usage']=report.get('usage')
                        if capture_patch(worktree,task['allowed_files'])!=patch: raise ValueError('Read-only reviewer changed project files')
                        okay=report['success']
                report_id=self._write(bid,directory/'report.json',json.dumps(dict(worker=result,local_review=report,criteria=task['acceptance_criteria'],criteria_status='pending_orchestrator_review'),indent=2))
                attempt.update(elapsed_seconds=round(time.monotonic()-started,3),status='verified' if okay else 'failed')
                entry.update(summary=report.get('final_summary','')[:2000],checks=checks,patch=patch_id,context=context_id,report=report_id,
                             required_artifacts=[report_id] if task['role']=='explore' else [patch_id,context_id,checks_id,report_id])
                for run in [result,report]:
                    if run.get('summary_truncated') and run.get('full_response_artifact') not in entry['required_artifacts']:
                        entry['required_artifacts'].append(run['full_response_artifact'])
                if cancel.is_set() or not self._update(bid,results=results): return
                if result.get('escalated'): raise RuntimeError('Worker requested clarification or a scope decision')
                if okay:
                    entry['status']='needs_review'; self._update(bid,results=results); break
                feedback=json.dumps(dict(error=result.get('error') or report.get('error'),checks=checks))
                for check in checks:
                    if not check['success']:
                        for key in ['stdout','stderr']:
                            if check.get(key): feedback+='\n'+Path(check[key]).read_text(encoding='utf-8',errors='replace')[-3000:]
                entry['status']='pending'; self._update(bid,results=results)
            if entry['status']!='needs_review': raise RuntimeError(f'Task {task["id"]} exhausted its {self.settings.max_attempts} attempts')
        if self._update(bid,'needs_review',results=results): self.store.event(bid,'batch_needs_review',task_ids=[t['id'] for t in results])
    def _integrate(self,bid):
        batch=self.store.get(bid); cancel=self.cancel_events.setdefault(bid,threading.Event())
        index=sum(e['event']=='review_approved' for e in self.store.events(bid))
        directory=self.root(bid)/f'integration-{index}'; worktree=directory/'worktree'
        create_worktree(Path(batch['repo_path']),worktree,batch['base'])
        files=[]; commands=[]
        for task,entry in zip(batch['tasks'],batch['results']):
            patch=Path(batch['artifacts'][entry['patch']]).read_bytes(); apply_patch(worktree,patch)
            files.extend(task['allowed_files'])
            for command in task['verification_commands']:
                if command not in commands: commands.append(command)
        baseline=capture_patch(worktree,files); start=time.monotonic()
        review_dir=directory/'review'
        source=context_text(worktree,files)
        if len(source)>32000: raise ValueError('Split this batch: integrated source exceeds the local review context budget')
        review=self.runner.run(worktree,'READ ONLY INTEGRATION REVIEW. Assess the captured source against these tasks, report findings, then STOP.\n'+json.dumps(batch['tasks'])+'\nCOMPLETE SOURCE\n'+source,review_dir,cancel,readonly=True,allowed_files=files,budget_tools=self.settings.max_tools,time_budget=self.settings.timeout_seconds)
        self._register_run(bid,review,review_dir)
        checks=self.runner.verify(worktree,commands,directory/'verification',cancel,time_budget=max(0,self.settings.timeout_seconds-(time.monotonic()-start))) if review['success'] else []
        for check in checks:
            for key in ['stdout','stderr']:
                if check.get(key) and Path(check[key]).is_file(): self._artifact(bid,check[key])
        if capture_patch(worktree,files)!=baseline: raise ValueError('Integration verification changed source files')
        required=[self._write(bid,directory/'diff.patch',baseline),self._write(bid,directory/'context.txt',context_text(worktree,files)),self._write(bid,directory/'checks.json',json.dumps(checks,indent=2)),self._write(bid,directory/'review.json',json.dumps(review,indent=2))]
        if review.get('summary_truncated'): required.append(review['full_response_artifact'])
        passed=review['success'] and len(checks)==len(commands) and all(c['success'] for c in checks)
        integration=dict(workspace=str(worktree),checks_passed=passed,required_artifacts=required,summary=review.get('final_summary','')[:600],local_seconds=round(time.monotonic()-start,3),tool_calls=review.get('tool_count',0))
        if cancel.is_set(): return
        if not passed:
            self._update(bid,'blocked',integration=integration,error='Integration verification failed'); return
        if self._update(bid,'final_review',integration=integration,error=None): self.store.event(bid,'integration_needs_review')
