import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {BrowserBridge} from '../bridge.mjs';
import {serveBrowser} from '../browser-driver.mjs';
import {atomicJson,readJson,pause} from '../storage.mjs';
async function temp(t) {const dir=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-bridge-'));t.after(()=>fs.rm(dir,{recursive:true,force:true}));return dir;}
test('bridge correlates replies and propagates structured failures',async t=>{
  const dir=await temp(t),bridge=new BrowserBridge(dir,{timeoutMs:2000});
  const pending=bridge.capture({job:'request'});
  await serveBrowser(dir,{capture:async()=>({entries:['captured']})},{durationMs:600});
  assert.deepEqual(await pending,{entries:['captured']});
  const failed=bridge.capture({}).catch(e=>e.message);
  await serveBrowser(dir,{capture:async()=>{throw Error('secret raw error');}},{durationMs:600});
  assert.equal(await failed,'browser_action_failed');
  assert(!(await fs.readFile(path.join(dir,'response.json'),'utf8')).includes('secret'));
});
test('stale responses cannot satisfy new commands',async t=>{
  const dir=await temp(t),bridge=new BrowserBridge(dir,{timeoutMs:250});
  await atomicJson(path.join(dir,'response.json'),{id:'old',ok:true,value:'stale'});
  await assert.rejects(bridge.inspect(),/browser_bridge_timeout/);
  assert.equal((await readJson(path.join(dir,'command.json'))).action,'idle');
});
test('expired browser commands never execute',async t=>{
  const dir=await temp(t);
  await atomicJson(path.join(dir,'command.json'),{id:'expired',action:'capture',expires:Date.now()-1});
  let called=0;
  await serveBrowser(dir,{capture:async()=>{called++;}},{durationMs:100});
  assert.equal(called,0);
});
