import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {latestIssueRequest,discoverLatestIssue} from '../../src/papertrack/browser/discovery.mjs';

test('latest discovery ignores ASAP, other journals and external links; sorts numerically',()=>{
  assert.deepEqual(latestIssueRequest('https://pubs.acs.org/loi/jcisd8','jcisd8',[
    '/jcisd8/issue/66/9','/toc/jcisd8/66/12','/jcisd8/issue/65/99',
    '/jacsat/issue/999/99','https://example.org/jcisd8/issue/999/99','/jcisd8/asap',
  ]),{url:'https://pubs.acs.org/jcisd8/issue/66/12',volume:'66',issue:'12'});
});
test('APS and AIP use canonical formal issue URLs',()=>{
  assert.equal(latestIssueRequest('https://journals.aps.org/prl/issues','prl',
    ['/prl/issues/136/1']).url,'https://journals.aps.org/prl/issues/136/1');
  assert.equal(latestIssueRequest('https://pubs.aip.org/aip/jcp/issue','jcp',
    ['/jcp/issue/164/16']).url,'https://pubs.aip.org/aip/jcp/issue/164/16');
});
test('challenge and missing archive links remain visible failures, with parser evidence',async(t)=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-discovery-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const options={evidencePath:path.join(root,'archive.html')};
  const html='<html><title>Archive</title><p>Changed layout</p></html>';
  const request={archiveUrl:'https://pubs.acs.org/loi/jcisd8',code:'jcisd8'};
  let observed={url:request.archiveUrl,title:'Just a moment...',links:[],challenge:true};
  const tab={playwright:{evaluate:async(fn)=>fn.name==='issueHtmlSnapshotDOM'?{length:html.length,chunks:[html]}:observed}};
  assert.equal((await discoverLatestIssue(tab,request,options)).status,'needs_access');
  observed={...observed,title:'Archive',challenge:false};
  assert.equal((await discoverLatestIssue(tab,request,options)).status,'needs_extractor_update');
  assert.equal(await fs.readFile(options.evidencePath,'utf8'),html);
  observed={...observed,readyState:'loading',bodyTextLength:0};
  assert.equal((await discoverLatestIssue(tab,request,options)).reason,'archive_not_loaded');
  observed={...observed,readyState:'complete'};
  assert.equal((await discoverLatestIssue(tab,request,options)).reason,'archive_not_loaded');
  observed={...observed,bodyTextLength:100};
  assert.equal((await discoverLatestIssue(tab,request,options)).status,'needs_extractor_update');
  observed.links=['/jcisd8/issue/66/12'];
  const result=await discoverLatestIssue(tab,request,options);
  assert.equal(result.status,'ready');
  assert.equal(result.request.issue,'12');
  observed.url='https://pubs.acs.org/jcisd8/issue-covers?ref=archive';
  assert.equal((await discoverLatestIssue(tab,request,options)).status,'ready');
  observed.url='https://pubs.acs.org/jacsat/issue-covers';
  assert.equal((await discoverLatestIssue(tab,request,options)).reason,'not_on_archive');
});
