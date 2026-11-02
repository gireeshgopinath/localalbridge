/** Explicit runtime setup; the bridge never loads or falls back to another model. */
import {LMStudioClient} from '@lmstudio/sdk';
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const directory=path.dirname(fileURLToPath(import.meta.url));
const args=process.argv.slice(2);
if (args.some(arg => !['--auto','--restore'].includes(arg)) && args[0]!=='--restore') {
  throw new Error('Usage: node load-profile.mjs [--auto | --restore backup.json]');
}
const restoring=args[0]==='--restore';
if ((restoring && args.length!==2) || (!restoring && args.length>1)) throw new Error('Invalid arguments');
const profile=JSON.parse(fs.readFileSync(restoring ? path.resolve(args[1]) : path.join(directory,'rtx5080-16k.json'),'utf8'));
if (profile.model!=='qwen3.8-27b' || profile.config.contextLength!==16384 || profile.config.maxParallelPredictions!==1) {
  throw new Error('Expected local qwen3.8-27b, context=16384, parallel=1');
}
if (args[0]==='--auto') {
  profile.config.llamaCppArgumentsOverride={enabled:false,disabledParameters:[],overrideParameters:[],excludeAllConfig:false};
}
const manual=!restoring && args[0]!=='--auto';
function gpuMemory() {
  const binary=process.platform==='win32' ? path.join(process.env.SystemRoot ?? 'C:/Windows','System32/nvidia-smi.exe') : 'nvidia-smi';
  const values=execFileSync(binary,['--id=0','--query-gpu=memory.total,memory.free','--format=csv,noheader,nounits'],{encoding:'utf8',windowsHide:true}).trim().split(',').map(Number);
  if (values.length!==2 || values.some(n => !Number.isFinite(n))) throw new Error('Cannot read GPU memory');
  return {totalMiB:values[0],freeMiB:values[1]};
}
if (manual && gpuMemory().totalMiB<16000) throw new Error('52-layer profile requires a 16GB NVIDIA GPU. Use --auto on other hardware.');

const client=new LMStudioClient({baseUrl:'ws://127.0.0.1:1234'});
async function nativeConfig() {
  const response=await fetch('http://127.0.0.1:1234/api/v1/models',{signal:AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error(`Local model inventory failed: ${response.status}`);
  const inventory=await response.json();
  return inventory.models.find(m=>m.key===profile.model)?.loaded_instances?.find(i=>i.id===profile.model)?.config;
}
const local=[];
for (const model of await client.llm.listLoaded()) {
  const info=await model.getModelInfo();
  if (info.deviceIdentifier!==null) continue;
  if (info.identifier!=='qwen3.8-27b') throw new Error('Another local language model is loaded. Unload it first.');
  const state=await model.getInstanceProcessingState();
  if (state.status!=='idle' || state.queued!==0) throw new Error('Model is busy. Finish/cancel active worker jobs before tuning.');
  local.push(model);
}
let previous=null;
if (local.length) {
  previous=await local[0].getLoadConfig();
  // Resolve speculative defaults from the server: SDK getLoadConfig can return inherited defaults.
  const actual=await nativeConfig();
  for (const [key,value] of Object.entries(actual ?? {})) {
    if (key.startsWith('speculative_')) previous[key.replace(/_([a-z])/g,(_,letter)=>letter.toUpperCase())]=value;
  }
  const backups=path.join(directory,'.runtime-backups'); fs.mkdirSync(backups,{recursive:true});
  const backup=path.join(backups,`${new Date().toISOString().replaceAll(':','-')}.json`);
  fs.writeFileSync(backup,JSON.stringify({model:profile.model,config:previous},null,2));
  console.log(`Previous load configuration: ${backup}`);
  await local[0].unload();
}
let loaded;
try {
  loaded=await client.llm.load(profile.model,{identifier:profile.model,deviceIdentifier:null,config:profile.config,signal:AbortSignal.timeout(180000),verbose:false});
  const actual=await loaded.getLoadConfig();
  if (actual.contextLength!==16384 || actual.maxParallelPredictions!==1) throw new Error('Runtime did not preserve 16K context and one slot');
  const effective=await nativeConfig();
  if (!effective || effective.context_length!==16384 || effective.parallel!==1) throw new Error('Native runtime context/parallel settings differ');
  if (!restoring && (effective.speculative_draft_mtp!==profile.config.speculativeDraftMtp || effective.speculative_draft_max_tokens!==profile.config.speculativeDraftMaxTokens)) {
    throw new Error('LM Studio ignored speculative settings. Enable MTP with three draft tokens in LM Studio, then retry.');
  }
  const memory=manual ? gpuMemory() : null;
  if (memory && memory.freeMiB<1024) throw new Error('Less than 1GiB GPU headroom. Use --auto or close GPU applications.');
  console.log(JSON.stringify({model:profile.model,context:actual.contextLength,parallel:actual.maxParallelPredictions,draftTokens:effective.speculative_draft_max_tokens,profile:restoring?'restored':manual?'rtx5080-16k':'automatic-placement',gpu:memory}));
} catch (error) {
  if (loaded) await loaded.unload();
  if (previous) {
    await client.llm.load(profile.model,{identifier:profile.model,deviceIdentifier:null,config:previous,signal:AbortSignal.timeout(180000),verbose:false});
    console.error('Previous load configuration restored.');
  }
  throw error;
}
