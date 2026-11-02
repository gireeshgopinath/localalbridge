"""Repeatable ten-task pilot. Preparation commits only a new synthetic fixture."""
import argparse
import ast
import json
import subprocess
from pathlib import Path

def prepare(root):
    root=Path(root).resolve()
    if root.exists(): raise ValueError('Choose a new empty pilot root; existing directories are never overwritten')
    repo=root/'repo';repo.mkdir(parents=True);(root/'hooks').mkdir()
    files={'README.md':'Synthetic Python project. baseline.double multiplies a number by two. Use unittest for tests.\n','baseline.py':'def double(n):\n    return n * 2\n','clamp.py':'def clamp(value, low, high):\n    return value\n','.gitignore':'__pycache__/\n.pytest_cache/\n'}
    for name,text in files.items():(repo/name).write_text(text,encoding='utf-8')
    cases=[
      ('add','add.py','Implement add(a, b), returning the sum of two numbers.',['Supports positive, negative and floating-point inputs.'],"from add import add; assert add(2,3)==5; assert add(-4,1)==-3; assert add(.5,.25)==.75"),
      ('clamp','clamp.py','Repair clamp(value, low, high) to constrain a number inclusively.',['Inside range stays unchanged; values outside return the nearest limit.','Raise ValueError when low > high.'],"from clamp import clamp; assert clamp(2,0,3)==2; assert clamp(-2,0,3)==0; assert clamp(7,0,3)==3; assert clamp(3,3,3)==3\ntry: clamp(1,3,0)\nexcept ValueError: pass\nelse: raise AssertionError('invalid bounds accepted')"),
      ('slug','slug.py','Implement slugify(text) for ASCII URL slugs.',['Lowercase ASCII letters/digits; replace each run of other characters with one hyphen; strip leading/trailing hyphens.'],"from slug import slugify; assert slugify('Hello, World!')=='hello-world'; assert slugify(' A___B  12 ')=='a-b-12'; assert slugify('!!!')==''"),
      ('bools','bools.py','Implement parse_bool(text).',['Trim whitespace and ignore case for true/false.','Return bool and raise ValueError for any other string.'],"from bools import parse_bool; assert parse_bool(' TRUE ') is True; assert parse_bool('False') is False\ntry: parse_bool('yes')\nexcept ValueError: pass\nelse: raise AssertionError('invalid boolean accepted')"),
      ('unique','unique.py','Implement stable_unique(values) for a list of integers.',['Return first occurrences in original order, without mutating input; empty input returns empty list.'],"from unique import stable_unique; a=[3,1,3,2,1]; assert stable_unique(a)==[3,1,2]; assert a==[3,1,3,2,1]; assert stable_unique([])==[]"),
      ('stats','stats.py','Implement mean(values) for a nonempty list of numbers.',['Return arithmetic mean; raise ValueError for empty input.'],"from stats import mean; assert mean([1,2,3])==2; assert mean([-2,2])==0; assert mean([.5,1.5])==1\ntry: mean([])\nexcept ValueError: pass\nelse: raise AssertionError('empty input accepted')"),
      ('tests','tests/test_baseline.py','Create unittest tests for baseline.double.',['At least three assertions covering zero, a positive number and a negative number.','Use unittest.TestCase and make discover -s tests pass.'],"import ast; from pathlib import Path; tree=ast.parse(Path('tests/test_baseline.py').read_text()); assert sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr.startswith('assert') for n in ast.walk(tree))>=3"),
      ('docs','USAGE.md','Document baseline.double with a short usage example.',['Explain multiplying a number by two and show double(3) producing 6.'],"from pathlib import Path; text=Path('USAGE.md').read_text(); assert 'double(3)' in text and '6' in text and len(text)>40"),
      ('paths','paths.py','Implement file_extension(path) returning the last filename extension including its dot.',['archive.tar.gz returns .gz; README and .bashrc return empty; folder/name.txt returns .txt.'],"from paths import file_extension; assert file_extension('archive.tar.gz')=='.gz'; assert file_extension('README')==''; assert file_extension('.bashrc')==''; assert file_extension('folder/name.txt')=='.txt'")]
    # The immutable oracle is outside each worker's approved edit scope.
    oracle='import sys\nCASES = '+repr({key:code for key,_,_,_,code in cases})+'\nexec(CASES[sys.argv[1]])\nprint("PASS", sys.argv[1])\n'
    (repo/'oracle.py').write_text(oracle,encoding='utf-8')
    tasks=[]
    for key,name,objective,criteria,code in cases:
        commands=[['python','oracle.py',key]]
        if key=='tests':commands.append(['python','-m','unittest','discover','-s','tests'])
        tasks.append(dict(id=key,objective=objective,allowed_files=[name],acceptance_criteria=criteria,verification_commands=commands,dependencies=[]))
    explore=dict(id='explore',role='explore',objective='Read README.md and baseline.py once. Identify the existing function and test convention in a short report. Do not edit.',allowed_files=[],acceptance_criteria=['Correctly identify baseline.double and unittest, without changes.'],verification_commands=[],dependencies=[])
    batches=[[explore],tasks[:3],tasks[3:6],tasks[6:]]
    git=['git','-c','core.hooksPath='+str(root/'hooks'),'-c','commit.gpgsign=false','-C',str(repo)]
    for args in [['init','-q'],['add','.'],['-c','user.name=Local worker pilot','-c','user.email=pilot@example.invalid','commit','-qm','Synthetic pilot baseline']]:subprocess.run(git+args,check=True,capture_output=True)
    specification=dict(repo_path=str(repo),batches=batches,expected_tasks=10)
    (root/'batches.json').write_text(json.dumps(specification,indent=2),encoding='utf-8')
    return specification

def report(state):
    from store import Store
    store=Store(Path(state));accepted=set();tasks=set();attempts=0;seconds=0;tools=0;decisions=[];details=[]
    with store.closing(store._conn()) as conn:ids=[r[0] for r in conn.execute('SELECT id FROM batches')]
    for bid in ids:
        batch=store.get(bid);tasks.update(t['id'] for t in batch['tasks'])
        if batch['status']=='completed':accepted.update(t['id'] for t in batch['tasks'])
        for result in batch.get('results',[]):
            attempts+=len(result.get('attempts',[]));seconds+=sum(a.get('elapsed_seconds',0) for a in result.get('attempts',[]));tools+=sum(a.get('tool_count',0) for a in result.get('attempts',[]))
            details.append(dict(id=result['id'],batch_id=bid,accepted=batch['status']=='completed',attempts=len(result.get('attempts',[])),checks_passed=all(c['success'] for c in result.get('checks',[])) if result.get('checks') else None))
        integration=batch.get('integration') or {}
        seconds+=integration.get('local_seconds',0);tools+=integration.get('tool_calls',0)
        decisions.extend(dict(batch_id=bid,event=e['event']) for e in store.events(bid) if e['event'] in {'review_approved','final_accepted'})
    return dict(accepted_tasks=len(accepted),submitted_tasks=len(tasks),accepted_ids=sorted(accepted),attempts=attempts,local_seconds=round(seconds,2),tool_calls=tools,cloud_tokens=None,token_usage_complete=False,orchestrator_interactions=sum(e.get('actor')=='orchestrator' for bid in ids for e in store.events(bid)),orchestrator_decisions=decisions,tasks=details,threshold_met=len(accepted)>=8 and len(tasks)==10)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','report']);parser.add_argument('--root',type=Path);parser.add_argument('--state',type=Path);args=parser.parse_args()
    if args.action=='prepare':
        if not args.root:parser.error('prepare requires --root')
        result=prepare(args.root)
    else:
        if not args.state:parser.error('report requires --state')
        result=report(args.state)
    print(json.dumps(result,indent=2))
