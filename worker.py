"""Local inference, OpenCode JSONL parsing, and independent verification."""
from __future__ import annotations
import json
import os
import re
import sys
import time
import uuid
import urllib.request
from pathlib import Path
from process_tools import run_process
from worker_config import build_config,child_environment

class Runner:
    def __init__(self,settings,on_process=None): self.settings=settings; self.on_process=on_process
    def _request(self,path,body=None,timeout=60):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url='http://127.0.0.1:1234'+path
        request=urllib.request.Request(url,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=timeout) as response: return json.load(response)
    def preflight(self,probe=False):
        try:
            models=self._request('/api/v1/models',timeout=5)['models']
            model=next(m for m in models if m.get('key')=='qwen3.8-27b')
            loaded=next(i for i in model.get('loaded_instances',[]) if i['id']=='qwen3.8-27b')
            config=loaded['config']
            if config['context_length']!=16384 or config.get('parallel',1)!=1: raise ValueError('Expected context=16384 and parallel=1')
            if probe:
                nonce=uuid.uuid4().hex[:12]
                body=dict(model='qwen3.8-27b',messages=[dict(role='user',content=f'Call echo with nonce {nonce}. Do not answer in text.')],
                          tools=[dict(type='function',function=dict(name='echo',description='Echo nonce',parameters=dict(type='object',properties=dict(nonce=dict(type='string')),required=['nonce'],additionalProperties=False)))],
                          tool_choice='required',parallel_tool_calls=False,max_tokens=160,temperature=0,reasoning_effort='none',chat_template_kwargs=dict(enable_thinking=False))
                result=self._request('/v1/chat/completions',body)
                calls=result['choices'][0]['message'].get('tool_calls',[])
                if not any(c['function']['name']=='echo' and json.loads(c['function']['arguments'])=={'nonce':nonce} for c in calls):
                    raise ValueError('Actual tool-call probe failed')
            return dict(model=self.settings.model,context=16384,parallel=1,tool_probe=bool(probe))
        except Exception as exc:
            raise RuntimeError(f'Local model unavailable or incompatible: {exc}. Load with: lms load qwen3.8-27b --context-length 16384 --parallel 1 --identifier qwen3.8-27b --yes') from exc
    def run(self,workspace,prompt,artifact_dir,cancel,readonly=False,allowed_files=None,verification_commands=None,budget_tools=None,time_budget=None):
        start=time.monotonic(); directory=Path(artifact_dir); directory.mkdir(parents=True,exist_ok=True)
        budget=min(self.settings.max_tools,budget_tools if budget_tools is not None else self.settings.max_tools)
        timeout=min(self.settings.timeout_seconds,time_budget if time_budget is not None else self.settings.timeout_seconds)
        output=dict(success=False,error=None,session_id=None,tool_count=0,elapsed_seconds=0,usage={'input':0,'output':0,'reasoning':0,'complete':False},cloud_tokens=None,final_summary='',log_paths=[])
        calls=set(); saw_text=False; texts=[]
        def line_received(line):
            nonlocal saw_text
            if not line.strip(): return
            try: event=json.loads(line)
            except json.JSONDecodeError as exc: raise ValueError('Malformed OpenCode JSONL output') from exc
            if not isinstance(event,dict) or event.get('type') not in {'step_start','step_finish','tool_use','text','reasoning','error'}:
                raise ValueError('Unknown OpenCode event')
            if event['type']=='error': raise RuntimeError(str(event.get('error',event))[:1000])
            output['session_id']=event.get('sessionID',output['session_id']); part=event.get('part',{})
            if event['type']=='tool_use':
                identity=part.get('id') or part.get('partID')
                if not identity: raise ValueError('Tool event has no identity')
                if identity not in calls:
                    calls.add(identity); output['tool_count']=len(calls)
                    if len(calls)>budget: raise RuntimeError('Tool-call budget exceeded')
                state=part.get('state',{})
                if state.get('status') in {'error','failed'} or state.get('error'): raise RuntimeError('Worker tool/permission failure: '+str(state.get('error',state.get('output','')))[:1000])
                # JSONL emits completed calls; stop at the boundary before further steps.
                if len(calls)>=budget: raise RuntimeError('Tool-call budget exhausted')
            elif event['type']=='step_finish':
                for key in ['input','output','reasoning']: output['usage'][key]+=int(part.get('tokens',{}).get(key,0) or 0)
            elif event['type']=='text':
                text=part.get('text',''); texts.append(text); saw_text=saw_text or bool(text.strip()); output['final_summary']=(''.join(texts))[:2000]
        try:
            if budget<=0 or timeout<=0: raise ValueError('No attempt budget remaining')
            self.preflight()
            config=build_config(self.settings,workspace,directory,allowed_files or [],readonly,budget)
            config_path=directory/'config'/'opencode.json'; config_path.parent.mkdir(exist_ok=True)
            config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
            prompt_path=directory/'prompt.txt'; prompt_path.write_text(prompt,encoding='utf-8')
            args=[self.settings.opencode_exe,'run','--standalone','--format','json','--model',self.settings.model,'--agent','worker','--file',str(prompt_path.resolve()),'Perform the attached task; finish immediately after the requested work.']
            remaining=timeout-(time.monotonic()-start)
            result=run_process(args,Path(workspace),directory/'process',cancel,remaining,on_line=line_received,on_process=self.on_process,env=child_environment(config_path.resolve(),config))
            output['log_paths']=[result['stdout'],result['stderr']]
            output['error']=result['error'] or (f'OpenCode exited {result["code"]}' if result['code']!=0 else None)
            if not output['error'] and not saw_text: output['error']='Worker returned no final text'
            output['success']=not bool(output['error'])
            if output['success'] and not (readonly and allowed_files) and texts and re.match(r'^\s*[#* ]*BLOCKED\b',texts[-1],re.I):
                output.update(success=False,error='Worker requested escalation',escalated=True)
        except Exception as exc: output['error']=str(exc)
        output['elapsed_seconds']=round(time.monotonic()-start,3)
        response_path=directory/'response.txt'
        response_path.write_text(''.join(texts),encoding='utf-8')
        output['full_response_path']=str(response_path.resolve())
        output['summary_truncated']=sum(map(len,texts))>2000
        (directory/'result.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
        return output
    def verify(self,workspace,commands,artifact_dir,cancel,time_budget=None):
        start=time.monotonic(); limit=min(self.settings.timeout_seconds,time_budget if time_budget is not None else self.settings.timeout_seconds); results=[]
        for index,command in enumerate(commands):
            actual=list(command)
            if actual[0].lower() in {'python','python.exe'}: actual[0]=sys.executable
            remaining=limit-(time.monotonic()-start)
            if remaining<=0 or cancel.is_set():
                results.append(dict(argv=command,actual_argv=actual,cwd=str(workspace),exit_code=None,error='cancelled' if cancel.is_set() else 'verification budget exhausted',success=False)); break
            env=os.environ.copy(); env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',PYTHONUTF8='1')
            result=run_process(actual,workspace,Path(artifact_dir)/str(index),cancel,remaining,on_process=self.on_process,env=env)
            results.append(dict(argv=command,actual_argv=actual,cwd=str(workspace),exit_code=result['code'],error=result['error'],success=result['code']==0 and not result['error'],duration=result['elapsed_seconds'],stdout=result['stdout'],stderr=result['stderr']))
        Path(artifact_dir).mkdir(parents=True,exist_ok=True)
        (Path(artifact_dir)/'checks.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        return results
