"""Codex MCP entrypoint. No OpenAI API key or cloud worker is used."""
import argparse
import asyncio
import json
from contextlib import asynccontextmanager,suppress
from dataclasses import replace
from pathlib import Path
from typing import Literal,Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from models import Settings
from engine import Engine
from worker import Runner

_engine=None
_settings=None
_sessions=[]
@asynccontextmanager
async def lifespan(server):
    global _engine
    loop=asyncio.get_running_loop()
    async def emit(event):
        for session in list(_sessions):
            with suppress(Exception): await session.send_log_message('info',event,logger='local-workers')
    def notify(event):
        loop.call_soon_threadsafe(lambda:asyncio.create_task(emit(event)))
    _engine=await asyncio.to_thread(Engine,_settings or Settings.from_env(),on_event=notify)
    try: yield {'engine':_engine}
    finally:
        await asyncio.to_thread(_engine.close); _engine=None; _sessions.clear()

mcp=FastMCP('local-workers',lifespan=lifespan,instructions=(
    'Use submit_batch for local exploration first, then batches of up to three small independent coding tasks. '
    'Workers execute only on local Qwen through OpenCode. Read every page of required artifacts and evaluate code and checks before decide_batch approve. '
    'First approval integrates; a second approval at final_review accepts the outcome. Passing tests never grants acceptance. '
    'Keep polling compact and bounded; full logs stay local. No commits, pushes, or deployments.'))
async def invoke(method,*args):
    if _engine is None: return {'error':'Bridge is not initialized'}
    session=mcp.get_context().session
    if not any(session is existing for existing in _sessions): _sessions.append(session)
    try: return await asyncio.to_thread(getattr(_engine,method),*args)
    except (ValueError,RuntimeError,OSError) as exc: return {'error':str(exc)}

@mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False))
async def submit_batch(repo_path:str,tasks:list[dict])->dict[str,Any]:
    """Queue approved tasks. Each needs id, objective, role, allowed_files, acceptance_criteria, verification_commands(argv lists), dependencies=[]. Requires a clean Git root. Returns immediately."""
    return await invoke('submit_batch',repo_path,tasks)
@mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,idempotentHint=True))
async def get_batch(batch_id:str)->dict[str,Any]:
    """Compact status, required artifact IDs, checks and usage. Completion still requires orchestrator approval."""
    return await invoke('get_batch',batch_id)
@mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=True,idempotentHint=True))
async def read_artifact(batch_id:str,artifact_id:str,cursor:int=0)->dict[str,Any]:
    """Read up to 12000 characters of registered evidence. Follow next_cursor until null before approval."""
    return await invoke('read_artifact',batch_id,artifact_id,cursor)
@mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False))
async def decide_batch(batch_id:str,action:Literal['approve','revise','stop'],feedback:str='')->dict[str,Any]:
    """Record the main orchestrator's decision after inspection. revise requires concrete feedback and remaining attempts."""
    return await invoke('decide_batch',batch_id,action,feedback)
@mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True))
async def cancel_batch(batch_id:str)->dict[str,Any]:
    """Cancel queued/running work, stop its owned processes, and preserve evidence."""
    return await invoke('cancel_batch',batch_id)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--state',type=Path); parser.add_argument('--doctor',action='store_true'); args=parser.parse_args()
    _settings=Settings.from_env()
    if args.state: _settings=replace(_settings,state_dir=args.state)
    if args.doctor:
        try: print(json.dumps(Runner(_settings).preflight(probe=True),indent=2))
        except Exception as exc: print(json.dumps({'error':str(exc)},indent=2)); raise SystemExit(1)
    else: mcp.run(transport='stdio')
