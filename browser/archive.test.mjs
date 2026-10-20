import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {collectCuaArchivePage} from '../src/papertrack/browser/cua.mjs';

test('archive capture retains raw HTML and exact URL without collecting issues',async(t)=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-archive-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const capturePath=path.join(dir,'capture.json');
  const html='<title>Volumes</title><a href="/prl/issues/136/1">9 January 2026</a>';
  let identity={url:'https://journals.aps.org/prl/issues/136',title:'Volumes',challenge:false};
  const tab={playwright:{evaluate:async(fn)=>fn.name==='issueHtmlSnapshotDOM'?
    {length:html.length,chunks:[html]}:identity}};
  const agent={browsers:{get:async()=>({tabs:{get:async()=>tab}})}};
  const request={url:identity.url};
  const output=await collectCuaArchivePage(agent,'browser','tab',request,{capturePath});
  assert.equal(output.status,'captured');
  assert.equal(await fs.readFile(capturePath+'.html','utf8'),html);
  assert.equal(JSON.parse(await fs.readFile(capturePath,'utf8')).schema,'papertrack-browser-archive-v1');
  await fs.unlink(capturePath);
  identity={...identity,challenge:true};
  assert.equal((await collectCuaArchivePage(agent,'browser','tab',request,{capturePath})).status,'needs_access');
  await assert.rejects(fs.stat(capturePath));
});
