import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {validateRequest,probeIssueAccess} from '../../src/papertrack/browser/access.mjs';
import {inspectSilverchairDOM} from '../../src/papertrack/browser/dom.mjs';
import {collectIssueBatch} from '../../src/papertrack/browser/collect.mjs';

test('h1 issue headings are returned by the same DOM parser accepted by access',async()=>{
  const oldDocument=globalThis.document,oldLocation=globalThis.location;
  const request={url:'https://pubs.acs.org/jcisd8/issue/66/12',volume:'66',issue:'12'};
  const heading={innerText:'Volume 66, Issue 12',textContent:'Volume 66, Issue 12'};
  const card={querySelector:()=>null,querySelectorAll:()=>[]};
  globalThis.location=new URL(request.url);
  globalThis.document={title:'Issue',getElementById:()=>null,querySelector:()=>null,
    querySelectorAll:selector=>selector.includes('h1')?[heading]:selector.includes('.al-article-item-wrap')?[card]:[]};
  try {
    assert.equal((await probeIssueAccess({playwright:{evaluate:async fn=>fn()}},request)).status,'ready');
    assert.deepEqual(inspectSilverchairDOM().headings,['Volume 66, Issue 12']);
  } finally {globalThis.document=oldDocument;globalThis.location=oldLocation;}
});

test('unsupported ACS toc aliases fail at the collection boundary',()=>{
  assert.throws(()=>validateRequest({url:'https://pubs.acs.org/toc/jcisd8/66/12',volume:'66',issue:'12'}),/invalid_issue_request/);
});

for(const fault of ['valid','missing','empty','tampered','wrong_doi','wrong_text','legacy']) {
  test(`APS resume checks raw evidence and source index: ${fault}`,async t=>{
    const folder=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-aps-evidence-'));
    t.after(()=>fs.rm(folder,{recursive:true,force:true}));
    const checkpoint=path.join(folder,'capture.json');
    const request={url:'https://journals.aps.org/prl/issues/136/1',volume:'136',issue:'1'};
    const doi='10.1103/example',text='Actual source abstract α.';
    const row={doi,title:'Paper',authors:['Author'],citation:'Phys. Rev. Lett. 136, 010101 (2026)',
      article_url:`/prl/abstract/${doi}`,abstract:'',abstract_type:'abstract'};
    let visits=0,current='';
    const tab={playwright:{evaluate:async fn=>{
      if(fn.name==='probeIssueDOM') return {url:request.url,title:'PRL',headings:['Volume 136, Issue 1'],cards:1,challenge:false};
      if(fn.name==='issueHtmlSnapshotDOM') {const html='<h1>Volume 136, Issue 1</h1>';return {length:html.length,chunks:[html]};}
      return {url:request.url,headings:['Volume 136, Issue 1'],entries:[structuredClone(row)]};
    }}};
    const detail={goto:async url=>{current=url;visits++;},playwright:{
      locator:()=>({count:async()=>0,waitFor:async()=>{},first:()=>({waitFor:async()=>{},click:async()=>{}})}),
      evaluate:async fn=>{
        if(fn.name==='issueHtmlSnapshotDOM') {
          const html=`<main><p>DOI: https://doi.org/${doi}</p><div id="abstract-section-content"><p>${text}</p></div></main>`;
          return {length:html.length,chunks:[html],reading:{url:current,doi:`DOI: https://doi.org/${doi}`,abstract:text,mathPending:false}};
        }
        return {url:current,doi:`DOI: https://doi.org/${doi}`,abstract:text};
      }
    }};
    assert.equal((await collectIssueBatch(tab,request,{checkpointPath:checkpoint,abstractTab:detail,saveHtml:true})).status,'complete');
    const base=path.join(checkpoint+'.articles',createHash('sha256').update(doi).digest('hex'));
    if(fault==='missing') await fs.unlink(base+'.html');
    if(fault==='empty') await fs.writeFile(base+'.html','');
    if(fault==='tampered') await fs.appendFile(base+'.html','unverified change');
    if(fault==='legacy') await fs.unlink(base+'.json');
    if(fault==='wrong_doi' || fault==='wrong_text') {
      const saved=JSON.parse(await fs.readFile(base+'.json','utf8'));
      if(fault==='wrong_doi') saved.reading.doi='DOI: 10.1103/other';
      else saved.reading.abstract='Wrong source text';
      await fs.writeFile(base+'.json',JSON.stringify(saved));
    }
    visits=0;
    const result=await collectIssueBatch(tab,request,{checkpointPath:checkpoint,abstractTab:detail,saveHtml:true,maxInteractions:1});
    assert.equal(result.status,'complete');
    assert.equal(visits,fault==='valid'?0:1);
    assert.equal(JSON.parse(await fs.readFile(checkpoint,'utf8')).entries[0].abstract,text);
    visits=0;
    await collectIssueBatch(tab,request,{checkpointPath:checkpoint,abstractTab:detail,saveHtml:true,maxInteractions:1});
    assert.equal(visits,0);
  });
}
