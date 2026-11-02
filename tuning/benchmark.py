"""Synthetic local speed/quality probe; generated test code runs with current-user access."""
import argparse, json, pathlib, re, time, urllib.request, subprocess, sys

p=argparse.ArgumentParser(); p.add_argument('profile'); p.add_argument('--model',default='qwen3.8-27b'); p.add_argument('--long',action='store_true'); p.add_argument('--quick',action='store_true'); a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',a.profile): p.error('profile must be a simple name')
out=pathlib.Path(__file__).parent/'.runtime-backups'/'benchmarks'/(a.profile+'.json')
out.parent.mkdir(parents=True,exist_ok=True)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:1234/api/v1/models',timeout=5) as response:
    inventory=json.load(response)
loaded=next((i for m in inventory['models'] for i in m.get('loaded_instances',[]) if i['id']==a.model),None)
if not loaded or loaded['config']['context_length']!=16384 or loaded['config'].get('parallel',1)!=1:
    p.error('Expected an already loaded local model with context=16384 and parallel=1')
cases=[('generation','Output the integers 1 through 80 separated by spaces. Nothing else.',160),
       ('code','Return only Python code, no markdown. Define merge_intervals(items) for pairs of integers. Return a NEW sorted list of merged inclusive intervals, merging overlaps and touching endpoints. Do not mutate input. Empty input returns [].',350)]
if a.quick: cases=cases[:1]
if a.long:
    lines=[f'item {i:04d} value alpha beta gamma delta' for i in range(1050)]
    lines[20]='SECRET_LEFT = amber-4821'; lines[1020]='SECRET_RIGHT = violet-7392'
    cases.append(('long_context','Read this data:\n'+'\n'.join(lines)+'\nReturn only SECRET_LEFT and SECRET_RIGHT values separated by a space.',60))
cases.append(('tool','Call lookup_file with path="example.py". Do not answer with text.',80))
results=[]
for name,prompt,limit in cases:
    data={'model':a.model,'messages':[{'role':'user','content':prompt}],'max_tokens':limit,'temperature':0,'reasoning_effort':'none','chat_template_kwargs':{'enable_thinking':False},'stream':True,'stream_options':{'include_usage':True}}
    if name=='tool':
        data['tools']=[{'type':'function','function':{'name':'lookup_file','description':'Read a local file','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}]
        data['tool_choice']='required'
    t=time.monotonic(); first=None; content=''; tools={}; usage={}; finish=None
    try:
        req=urllib.request.Request('http://127.0.0.1:1234/v1/chat/completions',data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=240) as resp:
            for raw in resp:
                line=raw.decode().strip()
                if not line.startswith('data: ') or line=='data: [DONE]': continue
                event=json.loads(line[6:]); usage=event.get('usage') or usage
                for choice in event.get('choices',[]):
                    delta=choice.get('delta',{}); txt=delta.get('content') or ''
                    if txt or delta.get('tool_calls'):
                        if first is None: first=time.monotonic()-t
                    content+=txt; finish=choice.get('finish_reason') or finish
                    for tc in delta.get('tool_calls',[]):
                        target=tools.setdefault(tc['index'],{'name':'','arguments':''})
                        f=tc.get('function',{})
                        target['name']+=f.get('name',''); target['arguments']+=f.get('arguments','')
        elapsed=time.monotonic()-t; ok=None
        if name=='code':
            src=re.sub(r'^```(?:python)?\s*|\s*```$','',content.strip())
            checks="""
f=merge_intervals
items=[(5,7),(1,3),(2,4),(9,9),(7,8)]; before=list(items)
assert [list(x) for x in f(items)] == [[1,4],[5,8],[9,9]]
assert items == before
assert f([]) == []
assert [list(x) for x in f([(1,2),(2,3)])] == [[1,3]]
"""
            completed=subprocess.run([sys.executable,'-I','-c',src+'\n'+checks],capture_output=True,text=True,timeout=5)
            ok=completed.returncode==0
        if name=='long_context': ok=content.strip()=='amber-4821 violet-7392'
        if name=='tool': ok=any(tc['name']=='lookup_file' and json.loads(tc['arguments'])=={'path':'example.py'} for tc in tools.values())
        n=usage.get('completion_tokens',0)
        row={'case':name,'elapsed_seconds':round(elapsed,3),'ttft_seconds':round(first,3) if first is not None else None,'usage':usage,'output_tokens_per_second':round((n-1)/(elapsed-first),2) if n>1 and first is not None and elapsed>first else None,'passed':ok,'finish_reason':finish,'output':content,'tools':list(tools.values())}
    except Exception as e:
        row={'case':name,'elapsed_seconds':round(time.monotonic()-t,3),'error':str(e),'output':content,'passed':False}
    results.append(row); out.write_text(json.dumps({'profile':a.profile,'model':a.model,'context_length':16384,'results':results},indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in row.items() if k not in ('output','tools')}),flush=True)
raise SystemExit(1 if any(r.get('passed') is False for r in results) else 0)
