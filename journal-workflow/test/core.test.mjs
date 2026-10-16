import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {executeIssue} from '../executor.mjs';
import {validateCapture,validateJob} from '../contracts.mjs';
import {runJob} from '../runner.mjs';
import {atomicJson,readJson} from '../storage.mjs';
const job={schema:'papertrack-journal-job-v1',journal:'jcim',url:'https://pubs.acs.org/jcisd8/issue/66/12',volume:'66',issue:'12',year:2026};
function capture(n=0) {return {schema:'papertrack-browser-issue-v1',url:job.url,volume:'66',issue:'12',headings:['Volume 66, Issue 12'],card_count:2,
  entries:[{doi:'10.1021/a',title:'A',abstract_type:'abstract',abstract:n>0?'One':''},
    {doi:'10.1021/b',title:'Correction',abstract_type:'extract',abstract:n>1?'Two':''}]};}
async function temp(t) {const dir=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-test-'));t.after(()=>fs.rm(dir,{recursive:true,force:true}));return dir;}
test('interruption persists first result; independent execution resumes the missing DOI',async t=>{
  const dir=await temp(t);let calls=0;
  await assert.rejects(executeIssue({job,dir,driver:{capture:async()=>{if(++calls===2)throw Error('disconnected');return capture(1);}}}),/disconnected/);
  assert.equal((await readJson(path.join(dir,'capture.json'))).entries[0].abstract,'One');
  const result=await executeIssue({job,dir,driver:{capture:async({previous})=>{assert.equal(previous.entries[0].abstract,'One');return capture(2);}}});
  assert.equal(result.status,'captured');assert.equal(result.captured,2);
});
test('identity drift and lost existing contents cannot overwrite checkpoint',async t=>{
  const dir=await temp(t),prior=capture(1);await atomicJson(path.join(dir,'capture.json'),prior);
  const changed=capture(2);changed.entries[0].doi='10.1021/wrong';
  await assert.rejects(executeIssue({job,dir,driver:{capture:async()=>changed}}),/inventory_drift/);
  assert.deepEqual(await readJson(path.join(dir,'capture.json')),prior);
  assert.throws(()=>validateCapture(capture(0),job,prior),/existing_content_changed/);
  assert.throws(()=>validateJob({...job,journal:'arxiv'}),/invalid_job/);
  assert.throws(()=>validateJob({...job,url:job.url+'?token=secret'}),/invalid_issue_url/);
});
test('Crawlee retries transient failures, publishes once, and completed rerun skips browser and agent',async t=>{
  const dir=await temp(t);let calls=0,published=0;
  const driver={capture:async()=>{if(++calls===1)throw Error('disconnected');return capture(2);}};
  const publish=async()=>{published++;const file=path.join(dir,'report.md');await fs.writeFile(file,'report');return {path:file,sha256:createHash('sha256').update('report').digest('hex')};};
  const advisor=async()=>assert.fail('model called on healthy retry');
  const result=await runJob({job,dir,output:dir,driver,publish,advisor});
  assert.equal(result.status,'complete');assert.equal(calls,2);
  assert((await fs.readdir(path.join(dir,'crawlee','request_queues'))).length>0);
  assert.equal((await runJob({job,dir,output:dir,driver,publish,advisor})).status,'complete');
  assert.equal(calls,2);assert.equal(published,1);
});
test('failed execution hands off to bounded agent port; invalid capture never publishes',async t=>{
  const dir=await temp(t);let advised=0;
  const result=await runJob({job,dir,output:dir,driver:{capture:async()=>{throw Error('no_progress');}},
    advisor:async()=>{advised++;return {action:'require_human',reason:'page_changed'};},
    publish:async()=>assert.fail('bad capture published')});
  assert.equal(result.status,'needs_attention');assert.equal(advised,1);
});
test('single writer lock rejects a competing run',async t=>{
  const dir=await temp(t);let enter;const entered=new Promise(r=>enter=r);let unblock;const blocked=new Promise(r=>unblock=r);
  const first=runJob({job,dir,output:dir,driver:{capture:async()=>{enter();await blocked;return capture(2);}},publish:async()=>({path:'test',sha256:'test'})});
  await entered;
  await assert.rejects(runJob({job,dir,output:dir}),error=>error.code==='ELOCKED');
  unblock();assert.equal((await first).status,'complete');
});
