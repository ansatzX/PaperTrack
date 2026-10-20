// Composition root: Crawlee owns scheduling/retries; execution does not know LLMs.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {execFile} from 'node:child_process';
import {createHash,randomUUID} from 'node:crypto';
import lockfile from 'proper-lockfile';
import {BasicCrawler} from '@crawlee/basic';
import {Configuration,RequestQueue,log} from '@crawlee/core';
import {executeIssue} from './executor.mjs';
import {BrowserBridge} from './bridge.mjs';
import {atomicJson,readJson} from './storage.mjs';
import {validateJob,keyOf} from './contracts.mjs';
const runFile=promisify(execFile);
const here=path.dirname(fileURLToPath(import.meta.url));
export async function renderReport({job,dir,output,python}) {
  const {stdout}=await runFile(python,[path.join(here,'render.py'),'--job',path.join(dir,'job.json'),
    '--capture',path.join(dir,'capture.json'),'--output',output],{timeout:60000,maxBuffer:100000});
  return JSON.parse(stdout);
}
export async function runJob({job,dir,output,python='python',driver,advisor=null,publish=renderReport}) {
  job=validateJob(job);dir=path.resolve(dir);output=path.resolve(output);
  await fs.mkdir(dir,{recursive:true,mode:0o700});
  const release=await lockfile.lock(dir,{realpath:false,retries:0,stale:10000,update:2000});
  try {
    const original=await readJson(path.join(dir,'job.json'));
    if(original && JSON.stringify(original)!==JSON.stringify(job)) throw Error('job_directory_mismatch');
    await atomicJson(path.join(dir,'job.json'),job);
    const priorStatus=await readJson(path.join(dir,'status.json'));
    if(priorStatus?.status==='complete' && priorStatus.outputRoot===output) {
      try {
        const hash=createHash('sha256').update(await fs.readFile(priorStatus.report.path)).digest('hex');
        if(hash===priorStatus.report.sha256) return priorStatus;
      } catch {}
      // Re-render validated saved content to preserve changed reading state.
      const report=await publish({job,dir,output,python});
      const status={...priorStatus,report};await atomicJson(path.join(dir,'status.json'),status);return status;
    }
    const bridge=driver || new BrowserBridge(dir);
    const config=new Configuration({storageClientOptions:{localDataDirectory:path.join(dir,'crawlee')},purgeOnStart:false,persistStorage:true});
    const queue=await RequestQueue.open('issues',{config});
    const runId=randomUUID();
    await queue.addRequest({url:job.url,uniqueKey:`${keyOf(job)}:${runId}`});
    let outcome={status:'running',runId,outputRoot:output};
    await atomicJson(path.join(dir,'status.json'),outcome);
    log.setLevel(log.LEVELS.ERROR);
    const crawler=new BasicCrawler({requestQueue:queue,maxConcurrency:1,maxRequestRetries:1,
      requestHandlerTimeoutSecs:1800,
      async requestHandler() {
        try {
          await executeIssue({job,dir,driver:bridge});
        } catch(error) {
          const failure=['no_progress','inventory_drift','existing_content_changed','invalid_capture','batch_budget_exhausted',
            'browser_bridge_timeout','browser_action_failed'].includes(error.message)?error.message:'capture_failed';
          await atomicJson(path.join(dir,'failure.json'),{failure,at:new Date().toISOString()});
          throw Error(failure);
        }
      },
      async failedRequestHandler() {outcome={...outcome,status:'needs_attention'};},
    },config);
    await crawler.run();
    if(outcome.status==='needs_attention' && advisor) {
      const failure=await readJson(path.join(dir,'failure.json'));
      const progress=await readJson(path.join(dir,'progress.json'));
      const decision=await advisor({tools:{inspect:()=>bridge.inspect()},
        evidence:{issue:keyOf(job),failure:failure?.failure,remaining:progress?.missing?.length}});
      await atomicJson(path.join(dir,'recovery.json'),decision);
      if(decision.action==='retry') {
        try {await executeIssue({job,dir,driver:bridge});outcome.status='running';}
        catch {outcome.status='needs_attention';}
      }
    }
    if(outcome.status==='running') {
      try {
        const report=await publish({job,dir,output,python});
        outcome={...outcome,status:'complete',report};
      } catch {outcome={...outcome,status:'needs_attention',failure:'report_validation_or_publish_failed'};}
    }
    await atomicJson(path.join(dir,'status.json'),outcome);
    return outcome;
  } finally {await release();}
}
