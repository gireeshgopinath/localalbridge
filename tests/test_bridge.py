import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from models import Settings,validate_tasks
from store import Store
from engine import Engine
from worker import Runner
from process_tools import run_process
from gitops import clean_base,create_worktree,capture_patch,apply_patch
from engine_support import artifact_text,StateLock

@pytest.fixture
def repo(tmp_path):
    root=tmp_path/'repo'; root.mkdir(); hooks=tmp_path/'hooks';hooks.mkdir()
    def git(*args): return subprocess.run(['git','-c','core.hooksPath='+str(hooks),'-c','commit.gpgsign=false','-C',str(root),*args],check=True,capture_output=True)
    git('init','-q'); (root/'seed.txt').write_text('baseline\n'); (root/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','Synthetic test fixture')
    return root

def task(**overrides):
    data=dict(id='one',objective='Create answer.py with answer = 42',allowed_files=['answer.py'],acceptance_criteria=['answer is 42'],verification_commands=[['python','-c','from answer import answer; assert answer == 42']])
    data.update(overrides);return data

def wait(engine,bid,statuses={'needs_review','blocked','final_review','completed','cancelled'}):
    until=time.monotonic()+15
    while time.monotonic()<until:
        value=engine.get_batch(bid)
        if value['status'] in statuses:return value
        time.sleep(.02)
    raise AssertionError(engine.get_batch(bid))

def inspect(engine,bid):
    batch=engine.get_batch(bid)
    ids=batch['integration']['required_artifacts'] if batch['status']=='final_review' else [a for t in batch['tasks'] for a in t['required_artifacts']]
    for identity in ids:
        cursor=0
        while True:
            page=engine.read_artifact(bid,identity,cursor);cursor=page['next_cursor']
            if cursor is None:break

class FakeRunner(Runner):
    def __init__(self,settings,bad=False,unavailable=False,slow=False):
        super().__init__(settings);self.bad=bad;self.unavailable=unavailable;self.slow=slow;self.calls=0
    def preflight(self,probe=False):
        if self.unavailable:raise RuntimeError('Local model unavailable')
        return {'tool_probe':probe}
    def run(self,workspace,prompt,artifact_dir,cancel,readonly=False,**kw):
        self.calls+=1
        if self.slow:cancel.wait(2)
        if not readonly and not cancel.is_set():(Path(workspace)/'answer.py').write_text('answer = '+('0' if self.bad else '42')+'\n')
        return dict(success=not cancel.is_set(),error='cancelled' if cancel.is_set() else None,tool_count=1,elapsed_seconds=.01,session_id='fake',usage={},final_summary='Verified locally; pending main review',log_paths=[])

@pytest.fixture
def settings(tmp_path):return Settings(state_dir=tmp_path/'state')

@pytest.mark.parametrize('name',['../x','C:foo','C:/foo','//host/x','a/./b','a//b','NUL.py','x:stream','a\nb','a\tb','.git/config','opencode.jsonc','.env','.env.local','private.pem','.ssh/id_rsa','credentials.json','x*'])
def test_invalid_paths(name):
    with pytest.raises(ValueError):validate_tasks([task(allowed_files=[name])])

def test_task_validation():
    assert validate_tasks([task()])[0].id=='one'
    for tasks in [[],[task(),task()],[task(),task(id='two',allowed_files=['ANSWER.py'])],[task(dependencies=['prior'])],[task(verification_commands=[])],[task(unknown_limit=1)]]:
        with pytest.raises(ValueError):validate_tasks(tasks)
    assert validate_tasks([task(role='explore',allowed_files=[],verification_commands=[])])
    with pytest.raises(ValueError):Settings(model='openai/anything')

def test_store_persistence_and_recovery(tmp_path):
    s=Store(tmp_path/'db'); bid=s.create({'pid':123,'status':'completed'});assert Store(tmp_path/'db').get(bid)['status']=='queued'
    s.event(bid,'submitted');s.update(bid,'running',extra=2);assert s.recover()==[bid]
    assert s.get(bid)['status']=='blocked' and s.get(bid)['pid']==123 and s.events(bid)[0]['event']=='submitted'
    s.update(bid,'needs_review');assert not s.recover()

def test_store_parallel_merges(tmp_path):
    s=Store(tmp_path/'db');bid=s.create({})
    threads=[threading.Thread(target=lambda i=i:s.update(bid,**{f'key{i}':i})) for i in range(20)]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    assert all(s.get(bid)[f'key{i}']==i for i in range(20))

def test_git_patch_and_scope(repo,tmp_path):
    root,head=clean_base(repo);a=tmp_path/'a';b=tmp_path/'b';create_worktree(root,a,head);create_worktree(root,b,head)
    (a/'new.py').write_text('answer = 42\n');patch=capture_patch(a,['new.py']);apply_patch(b,patch);assert (b/'new.py').read_text()=='answer = 42\n'
    assert not (root/'new.py').exists();clean_base(root)
    (a/'seed.txt').unlink();assert b'deleted file mode' in capture_patch(a,['new.py','seed.txt'])
    with pytest.raises(ValueError):capture_patch(a,['new.py'])
    (a/'new.py').write_bytes(b'\0binary')
    with pytest.raises(ValueError):capture_patch(a,['new.py','seed.txt'])
    (repo/'dirty.txt').write_text('x')
    with pytest.raises(ValueError):clean_base(repo)

def test_repo_root_only(repo,tmp_path):
    nested=repo/'folder';nested.mkdir()
    with pytest.raises(ValueError):clean_base(nested)
    with pytest.raises(ValueError):create_worktree(repo,repo/'inside',clean_base(repo)[1])

def test_state_lock(settings):
    lock=StateLock(settings.state_dir)
    try:
        with pytest.raises(RuntimeError):StateLock(settings.state_dir)
    finally:lock.close()
    StateLock(settings.state_dir).close()

def test_artifact_pages_and_redaction(tmp_path):
    root=tmp_path/'artifacts';root.mkdir();path=root/'a.txt';path.write_text('x'*11990+'\napi_key = "sk-'+'a'*40+'"\n'+'z'*13000)
    first=artifact_text(root,path);second=artifact_text(root,path,first['next_cursor'])
    assert 'sk-' not in first['content']+second['content'];assert '[REDACTED]' in first['content']+second['content']
    for cursor in [-1,999999,True]:
        with pytest.raises(ValueError):artifact_text(root,path,cursor)
    outside=tmp_path/'outside';outside.write_text('private')
    with pytest.raises(ValueError):artifact_text(root,outside)

def test_two_explicit_review_gates(repo,settings):
    engine=Engine(settings,FakeRunner(settings))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];assert wait(engine,bid)['status']=='needs_review'
        with pytest.raises(ValueError):engine.decide_batch(bid,'approve')
        with pytest.raises(ValueError):engine.read_artifact(bid,'../../outside')
        inspect(engine,bid);assert engine.decide_batch(bid,'approve')['status']=='integrating'
        assert wait(engine,bid)['status']=='final_review'
        with pytest.raises(ValueError):engine.decide_batch(bid,'approve')
        inspect(engine,bid);assert engine.decide_batch(bid,'approve')['status']=='completed'
        events=engine.store.events(bid);assert any(e['event']=='final_accepted' for e in events)
        assert not (repo/'answer.py').exists();assert engine.get_batch(bid)['metrics']['cloud_tokens'] is None
    finally:engine.close()

def test_false_worker_success_cannot_override_checks(repo,settings):
    engine=Engine(settings,FakeRunner(settings,bad=True))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];value=wait(engine,bid)
        assert value['status']=='blocked' and value['tasks'][0]['attempts']==3
        with pytest.raises(ValueError):engine.decide_batch(bid,'approve')
        with pytest.raises(ValueError):engine.decide_batch(bid,'revise','try again')
    finally:engine.close()

def test_unavailable_stops_without_execution(repo,settings):
    runner=FakeRunner(settings,unavailable=True);engine=Engine(settings,runner)
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];assert wait(engine,bid)['status']=='blocked';assert runner.calls==0
    finally:engine.close()

def test_cancellation_cannot_complete(repo,settings):
    engine=Engine(settings,FakeRunner(settings,slow=True))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];wait(engine,bid,{'running'})
        engine.cancel_batch(bid);time.sleep(.15);assert engine.get_batch(bid)['status']=='cancelled'
        with pytest.raises(ValueError):engine.decide_batch(bid,'approve')
    finally:engine.close()

def test_revision_requires_feedback(repo,settings):
    engine=Engine(settings,FakeRunner(settings))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];wait(engine,bid)
        with pytest.raises(ValueError):engine.decide_batch(bid,'revise','')
        engine.decide_batch(bid,'revise','Recheck answer is 42');assert wait(engine,bid)['tasks'][0]['attempts']==2
    finally:engine.close()

@pytest.mark.parametrize('mode',['timeout','cancel','callback'])
def test_process_failure_cleanup(tmp_path,mode):
    event=threading.Event();pid=[]
    if mode=='cancel':threading.Timer(.2,event.set).start()
    def line(s):
        if mode=='callback':raise ValueError('malformed output')
    result=run_process([sys.executable,'-u','-c','import time; print("ready"); time.sleep(30)'],tmp_path,tmp_path/mode,event,.25 if mode=='timeout' else 3,on_line=line,on_process=lambda p:pid.append(p))
    assert result['error'] and result['elapsed_seconds']<3
    assert pid[0] and pid[-1] is None
    assert Path(result['stdout']).is_file()

def test_process_exit_and_missing_executable(tmp_path):
    result=run_process([sys.executable,'-c','import sys;print("evidence");sys.exit(7)'],tmp_path,tmp_path/'exit',threading.Event(),5)
    assert result['code']==7 and not result['error'] and 'evidence' in Path(result['stdout']).read_text()
    result=run_process([str(tmp_path/'missing.exe')],tmp_path,tmp_path/'missing',threading.Event(),5);assert result['error']

def test_runner_rejects_bad_stream(monkeypatch,tmp_path,settings):
    runner=Runner(settings);monkeypatch.setattr(runner,'preflight',lambda **kw:{})
    def fake(argv,cwd,out_dir,cancel,timeout,**kw):
        out_dir.mkdir(parents=True);a=out_dir/'stdout.log';b=out_dir/'stderr.log';a.write_text('bad');b.write_text('')
        try:kw['on_line']('not JSON')
        except Exception as exc:return dict(code=1,error=str(exc),stdout=str(a),stderr=str(b))
    monkeypatch.setattr('worker.run_process',fake)
    result=runner.run(tmp_path,'task',tmp_path/'evidence',threading.Event());assert not result['success'] and 'Malformed' in result['error']

def test_local_profile_and_credentials(settings,tmp_path,monkeypatch):
    from worker_config import build_config,child_environment
    config=build_config(settings,tmp_path,tmp_path/'evidence',['answer.py'],False,30)
    assert config['model']=='lmstudio/qwen3.8-27b'
    assert all(a['model']==settings.model for a in config['agents'].values())
    assert config['providers']['lmstudio']['settings']['baseURL']=='http://127.0.0.1:1234/v1'
    monkeypatch.setenv('OPENAI_API_KEY','secret');assert 'OPENAI_API_KEY' not in child_environment(tmp_path/'config.json',config)

@pytest.mark.parametrize('status',['queued','running','integrating'])
def test_recovery_all_active_states(tmp_path,status):
    s=Store(tmp_path/'db');bid=s.create({'active_pid':12345});s.update(bid,status);assert s.recover()==[bid];assert s.get(bid)['status']=='blocked'

def test_git_stdin_disconnected(monkeypatch,tmp_path):
    import gitops
    seen={}
    def execute(argv,**kwargs):seen.update(kwargs);return subprocess.CompletedProcess(argv,0,b'ok',b'')
    monkeypatch.setattr(gitops.subprocess,'run',execute)
    assert gitops.run_git(tmp_path,'status')==b'ok';assert seen['stdin']==subprocess.DEVNULL
    seen.clear();gitops.run_git(tmp_path,'apply','-',input_bytes=b'patch');assert 'stdin' not in seen and seen['input']==b'patch'

@pytest.mark.parametrize('mode',['budget','permission','unknown','empty'])
def test_runner_fail_closed_streams(monkeypatch,tmp_path,settings,mode):
    runner=Runner(settings);monkeypatch.setattr(runner,'preflight',lambda **kw:{})
    def fake(argv,cwd,out_dir,cancel,timeout,**kw):
        out_dir.mkdir(parents=True);a=out_dir/'stdout.log';b=out_dir/'stderr.log';a.write_text('');b.write_text('')
        if mode=='budget':events=[dict(type='tool_use',part=dict(id=str(i),state=dict(status='completed'))) for i in range(3)]
        elif mode=='permission':events=[dict(type='tool_use',part=dict(id='x',state=dict(status='error',error='Permission denied')))]
        elif mode=='unknown':events=[dict(type='unsupported')]
        else:events=[]
        error=None
        try:
            for event in events:kw['on_line'](json.dumps(event))
        except Exception as exc:error=str(exc)
        return dict(code=0,error=error,stdout=str(a),stderr=str(b))
    monkeypatch.setattr('worker.run_process',fake)
    value=runner.run(tmp_path,'task',tmp_path/'evidence',threading.Event(),budget_tools=2)
    assert not value['success'] and value['error']

def test_descendants_killed_after_parent_exit(tmp_path):
    from engine_support import pid_alive
    code='import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)'
    result=run_process([sys.executable,'-c',code],tmp_path,tmp_path/'tree',threading.Event(),5)
    assert result['code']==0 and not result['error']
    pid=int(Path(result['stdout']).read_text().strip());time.sleep(.05);assert not pid_alive(pid)

def test_junction_rejected(repo,tmp_path):
    from engine_support import no_links
    target=tmp_path/'target';target.mkdir();link=tmp_path/'junction'
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True)
        if result.returncode:pytest.skip('Junction creation unavailable on this host')
    else:link.symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError):no_links(link/'file.txt')

def test_meaningful_notifications_only(repo,settings):
    events=[];engine=Engine(settings,FakeRunner(settings),on_event=events.append)
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];wait(engine,bid)
        assert [e['status'] for e in events]==['needs_review']
        engine.get_batch(bid);engine.get_batch(bid);assert len(events)==1
        inspect(engine,bid);engine.decide_batch(bid,'approve');wait(engine,bid)
        assert [e['status'] for e in events]==['needs_review','final_review']
    finally:engine.close()

def test_full_worker_response_preserved(monkeypatch,tmp_path,settings):
    runner=Runner(settings);monkeypatch.setattr(runner,'preflight',lambda **kw:{})
    def fake(argv,cwd,out_dir,cancel,timeout,**kw):
        out_dir.mkdir(parents=True);a=out_dir/'stdout.log';b=out_dir/'stderr.log';a.write_text('');b.write_text('')
        kw['on_line'](json.dumps(dict(type='text',part=dict(text='x'*2500))))
        return dict(code=0,error=None,stdout=str(a),stderr=str(b))
    monkeypatch.setattr('worker.run_process',fake)
    result=runner.run(tmp_path,'task',tmp_path/'evidence',threading.Event(),readonly=True,allowed_files=['x.py'])
    assert result['success'] and result['summary_truncated'] and len(result['final_summary'])==2000
    assert Path(result['full_response_path']).read_text()=='x'*2500

def test_readonly_reviewer_has_no_tools(settings,tmp_path):
    from worker_config import build_config
    c=build_config(settings,tmp_path,tmp_path/'evidence',['answer.py'],True,30)
    assert c['agents']['worker']['permissions']==[dict(action='*',resource='*',effect='deny')]
    assert c['policies'][-1]==dict(action='tool.use',resource='*',effect='deny')

def test_mcp_catalog_has_five_structured_tools():
    import asyncio,server
    tools=asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools}=={'submit_batch','get_batch','read_artifact','decide_batch','cancel_batch'}
    assert all(t.outputSchema for t in tools)

@pytest.mark.parametrize('text,readonly,success',[('Supports BLOCKED states in application logs.',False,True),('BLOCKED: missing requirements',False,False),('BLOCKED: criterion needs main review',True,True)])
def test_explicit_escalation_only(monkeypatch,tmp_path,settings,text,readonly,success):
    runner=Runner(settings);monkeypatch.setattr(runner,'preflight',lambda **kw:{})
    def fake(argv,cwd,out_dir,cancel,timeout,**kw):
        out_dir.mkdir(parents=True);a=out_dir/'stdout.log';b=out_dir/'stderr.log';a.write_text('');b.write_text('')
        kw['on_line'](json.dumps(dict(type='text',part=dict(text=text))))
        return dict(code=0,error=None,stdout=str(a),stderr=str(b))
    monkeypatch.setattr('worker.run_process',fake)
    value=runner.run(tmp_path,'task',tmp_path/'evidence',threading.Event(),readonly=readonly,allowed_files=['answer.py'])
    assert value['success'] is success

def test_escalation_stops_after_one_attempt(repo,settings):
    class Escalating(FakeRunner):
        def run(self,*args,**kwargs):
            result=super().run(*args,**kwargs);result.update(success=False,error='Worker requested escalation',escalated=True);return result
    engine=Engine(settings,Escalating(settings))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];value=wait(engine,bid)
        assert value['status']=='blocked' and value['tasks'][0]['attempts']==1
    finally:engine.close()

def test_truncated_reports_require_full_response_read(repo,settings):
    class LongReply(FakeRunner):
        def run(self,*args,**kwargs):
            result=super().run(*args,**kwargs);directory=Path(args[2]);directory.mkdir(parents=True,exist_ok=True)
            path=directory/'response.txt';path.write_text('x'*3000)
            result.update(full_response_path=str(path),summary_truncated=True,final_summary='x'*2000);return result
    engine=Engine(settings,LongReply(settings))
    try:
        bid=engine.submit_batch(str(repo),[task()])['batch_id'];value=wait(engine,bid)
        required=value['tasks'][0]['required_artifacts'];assert sum(x.endswith('response.txt') for x in required)==2
        for identity in required:
            if not identity.endswith('response.txt'):engine.read_artifact(bid,identity)
        with pytest.raises(ValueError):engine.decide_batch(bid,'approve')
        inspect(engine,bid);assert engine.decide_batch(bid,'approve')['status']=='integrating';wait(engine,bid)
    finally:engine.close()
