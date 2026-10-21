import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {probeIssueAccess,validateRequest} from '../../src/papertrack/browser/access.mjs';
import {collectIssueBatch,readIssueHtml} from '../../src/papertrack/browser/collect.mjs';
import {inspectSilverchairDOM} from '../../src/papertrack/browser/dom.mjs';

const request={url:'https://pubs.acs.org/jcisd8/issue/66/12',volume:'66',issue:'12'};
function harness() {
  const cards=['a','b'].map((id,i)=>({doi:`10.1021/${id}`,title:`Paper ${id}`,authors:['An Author'],
    article_url:`/jcisd8/article/66/12/1/${i}/paper`,citation:'JCIM (2026) 66 (12): 1–4.',
    control:`opaque-${id}`,panel:`different-${i}`,abstract_type:i?'extract':'abstract',abstract:'',expanded:false}));
  const calls=[];
  const state={blocked:false,failClick:false};
  const tab={goto:async()=>{throw Error('fixed collector must not navigate');},playwright:{
    evaluate:async (fn,arg)=> {
      if(fn.name==='probeIssueDOM') return {url:request.url,title:state.blocked?'Just a moment...':'Issue Table of Contents',
        headings:['Volume 66, Issue 12'],cards:cards.length,challenge:state.blocked};
      if(fn.name==='issueHtmlSnapshotDOM') {const html='<html>official rendered issue evidence</html>';return {length:html.length,chunks:[html]};}
      if(fn.name==='failedReadingSnapshotDOM') {
        calls.push('failure-evidence');
        const card=cards.find(c=>c.doi===arg.doi);
        return {schema:'papertrack-failed-reading-html-v1',url:request.url,doi:arg.doi,
          card_html:'<div>failed card</div>',panel_html:'<div>loading</div>',
          control:{id:arg.control,panel:arg.panel,expanded:String(card.expanded)}};
      }
      if(fn.name==='readingHtmlSnapshotDOM') return {schema:'papertrack-reading-html-v1',url:request.url,doi:arg.doi,
        card_html:`<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/${arg.doi}">DOI</a></span></div>`,
        panel_html:`<div id="${arg.panel}"><section class="abstract">Verbatim ${arg.doi} text</section></div>`};
      return {url:request.url,headings:['Volume 66, Issue 12'],cards:structuredClone(cards)};
    },
    locator:selector=>({getAttribute:async()=>String(cards.find(c=>'#'+c.control===selector).expanded),click:async()=>{
      calls.push(selector);
      if(state.failClick) throw Error('interaction failed');
      const card=cards.find(c=>'#'+c.control===selector);
      if(cards.some(c=>c.expanded&&c!==card)) throw Error('overlay remains open');
      card.expanded=!card.expanded;card.abstract=card.expanded&&!state.noLoad?`Verbatim ${card.doi} text`:'';
    }})
  }};
  return {tab,cards,calls,state};
}
async function fixture(t) {
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'papertrack-fixed-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  return path.join(root,'capture.json');
}

test('access gate reports a challenge without navigating or clicking it',async()=>{
  const h=harness();h.state.blocked=true;
  assert.equal((await probeIssueAccess(h.tab,request)).status,'needs_access');
  assert.equal(h.calls.length,0);
});
test('formal request rejects arbitrary URLs and placement mismatch',()=>{
  for(const url of ['https://example.org/jcisd8/issue/66/12','https://pubs.acs.org/jcisd8/issue/66/13','http://pubs.acs.org/jcisd8/issue/66/12']) {
    assert.throws(()=>validateRequest({...request,url}),/invalid_issue_request/);
  }
});
test('publisher-labelled partial APS issue is not an access failure or a complete issue',async t=>{
  const checkpointPath=await fixture(t);
  const aps={url:'https://journals.aps.org/prl/issues/137/12',volume:'137',issue:'12'};
  let heading='Volume 137, Issue 12 (partial)';
  const tab={playwright:{evaluate:async fn=>{
    if(fn.name==='probeIssueDOM') return {url:aps.url,title:'Physical Review Letters',
      headings:[heading],cards:70,challenge:false};
    if(fn.name==='issueHtmlSnapshotDOM') {
      const html=`<h1>${heading}</h1>`;
      return {length:html.length,chunks:[html]};
    }
    throw Error('Must not collect a publisher-labelled partial issue');
  }}};
  const result=await collectIssueBatch(tab,aps,{checkpointPath,saveHtml:true});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.access.reason,'publisher_partial_issue');
  assert.match(await fs.readFile(checkpointPath+'.access.html','utf8'),/Issue 12 \(partial\)/);
  await assert.rejects(fs.access(checkpointPath),{code:'ENOENT'});
  heading='Volume 137, Issue 12';
  assert.equal((await probeIssueAccess(tab,aps)).status,'ready');
  heading='Volume 137, Issue 13 (partial)';
  assert.notEqual((await probeIssueAccess(tab,aps)).status,'ready');
});
test('DOM parser follows actual aria-controls for opaque IDs and prefers current wrappers',()=>{
  const priorDocument=globalThis.document,priorLocation=globalThis.location;
  const main={innerText:'Verbatim full abstract, not the teaser.'};
  const control={id:'completely-opaque-control',getAttribute:key=>({'aria-controls':'actual-panel-target','data-abstract-type':'abstract','aria-expanded':'true'}[key])};
  const title={innerText:'Paper',getAttribute:()=>'/jcisd8/article/66/12/1/42/paper'};
  const card={querySelector:selector=>({'h3.item-title a':title,'.showAbstractLink':control,
    '.citation-label a':{getAttribute:()=> 'https://doi.org/10.1021/a'},'.ww-citation-primary':{innerText:'JCIM (2026) 66 (12): 1–4.'}}[selector]),
    querySelectorAll:selector=>selector==='.showAbstractLink'?[control]:[{innerText:'An Author'}]};
  try {
    globalThis.location={href:request.url};
    globalThis.document={querySelectorAll:selector=>selector==='.al-article-item-wrap'?[card]:selector.startsWith('option')?[]:[{innerText:'Volume 66, Issue 12'}],
      getElementById:id=>{assert.equal(id,'actual-panel-target');return {getAttribute:()=> 'false',querySelector:()=>main};}};
    const data=inspectSilverchairDOM();assert.equal(data.cards.length,1);
    assert.equal(data.cards[0].abstract,main.innerText);
    assert.equal(data.cards[0].panel,'actual-panel-target');
  } finally {globalThis.document=priorDocument;globalThis.location=priorLocation;}
});
test('bounded checkpoints resume across fresh tabs and keep HTML and prior text on access loss',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  const first=await collectIssueBatch(h.tab,request,{checkpointPath,maxInteractions:1,saveHtml:true});
  assert.equal(first.status,'partial');assert.equal(first.captured,1);
  const prior=await fs.readFile(checkpointPath,'utf8');
  const html=await fs.readFile(checkpointPath+'.html','utf8');
  h.state.blocked=true;
  assert.equal((await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true})).status,'needs_access');
  assert.equal(await fs.readFile(checkpointPath,'utf8'),prior);
  assert.equal(await fs.readFile(checkpointPath+'.html','utf8'),html);
  const fresh=harness();fresh.cards.reverse();
  const second=await collectIssueBatch(fresh.tab,request,{checkpointPath});
  assert.equal(second.status,'complete');assert.equal(second.captured,2);
  assert.deepEqual(fresh.calls,['#opaque-b','#opaque-b']);
  const data=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  assert.equal(data.collector,'papertrack-browser-v2');assert.equal(data.capture_method,'fixed_browser');
  assert.equal(data.entries.find(e=>e.doi.endsWith('/a')).abstract,'Verbatim 10.1021/a text');
});
test('bad checkpoint and changed inventory cannot replace saved evidence',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  await collectIssueBatch(h.tab,request,{checkpointPath,maxInteractions:1});
  const prior=await fs.readFile(checkpointPath,'utf8');
  h.cards[1].doi='10.1021/other';
  await assert.rejects(collectIssueBatch(h.tab,request,{checkpointPath}),/different issue inventory/);
  assert.equal(await fs.readFile(checkpointPath,'utf8'),prior);
  await fs.writeFile(checkpointPath,'not JSON');
  await assert.rejects(collectIssueBatch(h.tab,request,{checkpointPath}),SyntaxError);
  assert.equal(await fs.readFile(checkpointPath,'utf8'),'not JSON');
});
test('failed interaction produces an actionable stage and recovers without inventing unavailable text',async t=>{
  const checkpointPath=await fixture(t),h=harness();h.state.failClick=true;
  const first=await collectIssueBatch(h.tab,request,{checkpointPath,maxInteractions:1});
  assert.equal(first.status,'needs_attention');assert.equal(first.errors[0].stage,'open');
  assert.equal(first.errors[0].message,'interaction failed');
  assert.equal(first.captured,0);assert.equal(first.remaining,2);
  h.state.failClick=false;
  const second=await collectIssueBatch(h.tab,request,{checkpointPath});
  assert.equal(second.status,'complete');assert.equal(second.errors.length,0);
  assert.equal(JSON.parse(await fs.readFile(checkpointPath,'utf8')).error_history.length,1);
});

test('CUA adapter binds only the supplied browser/tab and never uses a different REPL transport',async t=>{
  const {collectCuaIssueBatch}=await import('../../src/papertrack/browser/cua.mjs');
  const checkpointPath=await fixture(t),h=harness(),bound=[];
  let closed=0;h.tab.close=async()=>{closed++;};
  const agent={browsers:{get:async id=>{bound.push(id);return {tabs:{get:async tabId=>{bound.push(tabId);return h.tab;}}};}}};
  const result=await collectCuaIssueBatch(agent,'selected-browser','owned-tab',request,{checkpointPath});
  assert.equal(result.status,'complete');assert.deepEqual(bound,['selected-browser','owned-tab']);
  assert.equal(closed,1);assert.equal(result.task_tabs_closed,true);
  await assert.rejects(collectCuaIssueBatch(undefined,'selected-browser','owned-tab',request,{checkpointPath}),/cua_browser_runtime_required/);
});


test('HTML export avoids capped large string results and rejects truncated chunks',async()=>{
  const html='<html>'+('rendered evidence '.repeat(20000))+'</html>';
  const priorDocument=globalThis.document;
  let reads=0,snapshot;
  globalThis.document={documentElement:{get outerHTML(){reads++;return html;}}};
  const tab={playwright:{evaluate:async fn=>{snapshot=fn();return snapshot;}}};
  try {
  assert.equal(await readIssueHtml(tab),html);
  assert.equal(reads,1);
  assert.ok(snapshot.chunks.length>1&&snapshot.chunks.every(c=>c.length<=32000));
  snapshot.chunks[0]=snapshot.chunks[0].slice(1);
  tab.playwright.evaluate=async()=>snapshot;
  await assert.rejects(readIssueHtml(tab),/html_snapshot_chunk_truncated/);
  } finally {globalThis.document=priorDocument;}
});

test('failed HTML export preserves prior source evidence',async t=>{
  const {saveIssueHtml}=await import('../../src/papertrack/browser/collect.mjs');
  const target=(await fixture(t))+'.html';await fs.writeFile(target,'prior valid source');
  const tab={playwright:{evaluate:async()=>({length:40000,chunks:['truncated']})}};
  await assert.rejects(saveIssueHtml(tab,target),/html_snapshot_chunk_truncated/);
  assert.equal(await fs.readFile(target,'utf8'),'prior valid source');
});

test('HTML export reads the live DOM once even when subsequent reads would change',async()=>{
  const html='<html>official archive</html>';
  let reads=0;
  const priorDocument=globalThis.document;
  globalThis.document={documentElement:{get outerHTML(){return ++reads===1?html:'changed';}}};
  try {
    assert.equal(await readIssueHtml({playwright:{evaluate:async fn=>fn()}}),html);
    assert.equal(reads,1);
  } finally {globalThis.document=priorDocument;}
});

test('APS source HTML and paragraph text come from a single snapshot without figure controls',async t=>{
  const {saveIssueHtml}=await import('../../src/papertrack/browser/collect.mjs');
  const target=(await fixture(t))+'.html';
  const oldDocument=globalThis.document,oldLocation=globalThis.location;
  let calls=0;
  const paragraph={textContent:'Actual abstract α.',querySelector:()=>null};
  const html='<main><p>DOI: 10.1103/example</p><div id="abstract-section-content"><p>Actual abstract α.</p><button>Close</button></div></main>';
  globalThis.location={href:'https://journals.aps.org/prl/abstract/10.1103/example'};
  globalThis.document={documentElement:{outerHTML:html},querySelector:()=>null,querySelectorAll:selector=>
    selector==='#abstract-section-content > p'?[paragraph]:[{textContent:'DOI: 10.1103/example'}]};
  try {
    const result=await saveIssueHtml({playwright:{evaluate:async(fn,arg)=>{calls++;return fn(arg);}}},target,{apsAbstract:true});
    assert.equal(calls,1);
    assert.equal(result.reading.abstract,'Actual abstract α.');
    assert.equal(result.reading.mathPending,false);
    assert.equal(await fs.readFile(target,'utf8'),html);
  } finally {globalThis.document=oldDocument;globalThis.location=oldLocation;}
});

test('old text checkpoints acquire missing panel evidence in bounded batches without losing text',async t=>{
  const checkpointPath=await fixture(t);
  const original=await collectIssueBatch(harness().tab,request,{checkpointPath});
  assert.equal(original.status,'complete');
  const first=await collectIssueBatch(harness().tab,request,{checkpointPath,saveHtml:true,maxInteractions:1});
  assert.equal(first.status,'partial');
  assert.equal(first.remaining,1);
  let data=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  assert.ok(data.entries.every(e=>e.abstract));
  const final=await collectIssueBatch(harness().tab,request,{checkpointPath,saveHtml:true,maxInteractions:1});
  assert.equal(final.status,'complete');
  assert.equal(first.captured,original.captured);
  assert.equal(final.captured,first.captured);
  assert.ok(final.remaining < first.remaining);
  assert.equal((await fs.readdir(checkpointPath+'.readings')).length,2);
});

test('failed panel evidence save cannot count a new abstract as captured',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>{
    if(fn.name==='failedReadingSnapshotDOM') {
        h.calls.push('failure-evidence');
        const card=h.cards.find(c=>c.doi===arg.doi);
        return {schema:'papertrack-failed-reading-html-v1',url:request.url,doi:arg.doi,
          card_html:'<div>failed card</div>',panel_html:'<div>loading</div>',
          control:{id:arg.control,panel:arg.panel,expanded:String(card.expanded)}};
      }
      if(fn.name==='readingHtmlSnapshotDOM') throw Error('panel evidence unavailable');
    return evaluate(fn,arg);
  };
  const result=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true,maxInteractions:1});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.remaining,2);
  assert.equal(result.captured,0);
  assert.equal(result.errors[0].evidence_error,undefined);
  assert.ok(result.errors[0].evidence);
});

test('issue-to-recent workflow retains formal membership and writes a separate supplement',async t=>{
  const {collectRecentBatch}=await import('../../src/papertrack/browser/collect.mjs');
  const checkpointPath=await fixture(t),issue=harness();
  const recentUrl='https://pubs.acs.org/jcisd8/latest-articles';
  issue.cards[0].abstract_type='unavailable';
  issue.cards[0].unavailable_reason='No reading control';
  const evaluateIssue=issue.tab.playwright.evaluate;
  issue.tab.playwright.evaluate=async(fn,arg)=>fn.name==='recentLinkDOM'?recentUrl:evaluateIssue(fn,arg);
  const pending=await collectIssueBatch(issue.tab,request,{checkpointPath,saveHtml:true});
  assert.equal(pending.status,'needs_recent');
  const before=await fs.readFile(checkpointPath,'utf8');
  const recent=harness(),evaluateRecent=recent.tab.playwright.evaluate;
  recent.tab.playwright.evaluate=async(fn,arg)=>{
    if(fn.name==='recentPageIdentityDOM') return {url:recentUrl,title:'Latest Articles',challenge:false,next_url:null};
    const value=await evaluateRecent(fn,arg);
    if(value.url) value.url=recentUrl;
    return value;
  };
  const result=await collectRecentBatch(recent.tab,request,{checkpointPath,recentUrl});
  assert.equal(result.status,'complete');
  assert.deepEqual(recent.calls,['#opaque-a','#opaque-a']);
  assert.equal(await fs.readFile(checkpointPath,'utf8'),before);
  const supplement=JSON.parse(await fs.readFile(checkpointPath+'.recent.json','utf8'));
  assert.deepEqual(supplement.entries.map(e=>e.doi),['10.1021/a']);
  assert.equal(supplement.complete,true);
  assert.equal((await fs.readdir(checkpointPath+'.recent-pages')).some(x=>x.endsWith('.readings')),true);
});

test('recent pages without the issue DOI remain unresolved, not empty success',async t=>{
  const {collectRecentBatch}=await import('../../src/papertrack/browser/collect.mjs');
  const checkpointPath=await fixture(t),h=harness();
  const recentUrl='https://pubs.acs.org/jcisd8/latest-articles';
  await fs.writeFile(checkpointPath,JSON.stringify({...request,recent_url:recentUrl,
    schema:'papertrack-browser-issue-v1',inventory_complete:true,errors:[],remaining_abstracts:0,
    entries:[{doi:'10.1021/not-on-page',title:'Formal issue member',abstract_type:'unavailable'}]}));
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>{
    if(fn.name==='recentPageIdentityDOM') return {url:recentUrl,title:'Latest Articles',challenge:false,next_url:null};
    const value=await evaluate(fn,arg);if(value.url)value.url=recentUrl;return value;
  };
  const result=await collectRecentBatch(h.tab,request,{checkpointPath,recentUrl});
  assert.equal(result.status,'needs_attention');
  assert.deepEqual(result.remaining_dois,['10.1021/not-on-page']);
  assert.deepEqual(h.calls,[]);
});


test('timeout retains independent failure evidence before closing the panel',async t=>{
  const checkpointPath=await fixture(t),h=harness();h.state.noLoad=true;
  const result=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true,maxInteractions:1,abstractTimeoutMs:1});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.errors[0].reason,'abstract_not_loaded');
  assert.deepEqual(h.calls,['#opaque-a','failure-evidence','#opaque-a']);
  const file=result.errors[0].evidence;
  assert.equal(path.dirname(file),checkpointPath+'.reading-failures');
  const evidence=JSON.parse(await fs.readFile(file,'utf8'));
  assert.equal(evidence.schema,'papertrack-failed-reading-html-v1');
  assert.equal(evidence.control.expanded,'true');
  assert.equal(evidence.failure.reason,'abstract_not_loaded');
  assert.equal(evidence.doi,'10.1021/a');
  await assert.rejects(fs.access(checkpointPath+'.readings'),{code:'ENOENT'});
  const capture=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  assert.equal(capture.entries[0].abstract,'');
  assert.equal(capture.remaining_abstracts,2);
  assert.equal(capture.errors.length,1);
});

test('failed sidecar write cannot replace the original interaction error',async t=>{
  const checkpointPath=await fixture(t),h=harness();h.state.failClick=true;
  await fs.writeFile(checkpointPath+'.reading-failures','obstruct directory creation');
  const result=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true,maxInteractions:1});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.errors[0].reason,'abstract_interaction_failed');
  assert.equal(result.errors[0].message,'interaction failed');
  assert.equal(result.errors[0].stage,'open');
  assert.match(result.errors[0].evidence_error,/EEXIST/);
  assert.equal(result.errors[0].evidence,undefined);
});

test('failed DOM snapshot is bounded and records missing controls without claiming text',async t=>{
  const {saveFailedReadingEvidence}=await import('../../src/papertrack/browser/reading.mjs');
  const checkpointPath=await fixture(t);
  const oldDocument=globalThis.document,oldLocation=globalThis.location;
  try {
    globalThis.location={href:request.url};
    const card={outerHTML:'x'.repeat(60000),querySelector:()=>({getAttribute:()=> 'https://doi.org/10.1021/a'})};
    globalThis.document={querySelectorAll:()=>[card],getElementById:()=>null};
    const tab={playwright:{evaluate:async(fn,arg)=>fn(arg)}};
    const file=await saveFailedReadingEvidence(tab,checkpointPath,{doi:'10.1021/a',control:'c',panel:'p'},
      {reason:'abstract_not_loaded',stage:'read'});
    const evidence=JSON.parse(await fs.readFile(file,'utf8'));
    assert.equal(evidence.card_html.length,48000);
    assert.equal(evidence.truncated.card,true);
    assert.equal(evidence.panel_html,'');
    assert.equal(evidence.control.found,false);
    assert.equal(evidence.control.panel_found,false);
    assert.equal(evidence.abstract,undefined);
  } finally {globalThis.document=oldDocument;globalThis.location=oldLocation;}
});

test('missing recent link remains unresolved and can resume when a link is observed',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  h.cards[0].abstract_type='unavailable';
  h.cards[0].unavailable_reason='No reading control';
  let link=null;
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>fn.name==='recentLinkDOM'?link:evaluate(fn,arg);
  const missing=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true});
  assert.equal(missing.status,'needs_attention');
  assert.equal(missing.reason,'recent_link_unavailable');
  assert.deepEqual(missing.remaining_dois,['10.1021/a']);
  const capture=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  assert.deepEqual(capture.errors[0],{reason:'recent_link_unavailable',stage:'recent_discovery',url:request.url,
    target_dois:['10.1021/a'],expected_recent_path:'/jcisd8/latest-articles',observed_recent_url:null});
  assert.equal(capture.error_history.at(-1).reason,'recent_link_unavailable');
  await fs.access(checkpointPath+'.html');
  link='https://pubs.acs.org/jcisd8/latest-articles';
  const resumed=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true});
  assert.equal(resumed.status,'needs_recent');
  assert.deepEqual(resumed.errors,[]);
  assert.equal(JSON.parse(await fs.readFile(checkpointPath,'utf8')).error_history.at(-1).reason,'recent_link_unavailable');
});

test('non-article unavailable cards do not require a recent link',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  h.cards[0].abstract_type='unavailable';
  h.cards[0].title='Editorial Masthead';
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>{
    assert.notEqual(fn.name,'recentLinkDOM');return evaluate(fn,arg);
  };
  const result=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:true});
  assert.equal(result.status,'complete');
  assert.deepEqual(result.errors,[]);
});

test('APS proven article without Abstract completes without Silverchair recent discovery',async t=>{
  const checkpointPath=await fixture(t);
  const aps={url:'https://journals.aps.org/prl/issues/137/13',volume:'137',issue:'13'};
  const row={doi:'10.1103/34df-l36k',title:'Erratum: Official correction',authors:['Author'],
    citation:'Phys. Rev. Lett. 137, 139901 (2026)',article_url:'/prl/abstract/10.1103/34df-l36k',
    abstract:'',abstract_type:'abstract'};
  const tab={playwright:{evaluate:async fn=>{
    assert.notEqual(fn.name,'recentLinkDOM');
    if(fn.name==='probeIssueDOM') return {url:aps.url,title:'Issue',headings:['Volume 137, Issue 13'],cards:1,challenge:false};
    if(fn.name==='issueHtmlSnapshotDOM') return {length:13,chunks:['<html></html>']};
    return {url:aps.url,headings:['Volume 137, Issue 13'],entries:[structuredClone(row)]};
  }}};
  let detailUrl;
  const abstractTab={goto:async url=>{detailUrl=url;},playwright:{evaluate:async fn=>{
    const reading={url:detailUrl,doi:row.doi,abstract:'',unavailableArticle:true};
    if(fn.name==='issueHtmlSnapshotDOM') return {length:13,chunks:['<html></html>'],reading};
    return reading;
  }}};
  const result=await collectIssueBatch(tab,aps,{checkpointPath,abstractTab,saveHtml:true});
  assert.equal(result.status,'complete');
  assert.deepEqual(result.errors,[]);
  assert.equal(result.recent_url,undefined);
  const capture=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  assert.equal(capture.entries[0].abstract_type,'unavailable');
  assert.match(capture.entries[0].unavailable_reason,/Official article page/);
});

test('AIP missing reading control still requires a discoverable recent link',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  const aip={...request,url:'https://pubs.aip.org/aip/jcp/issue/66/12'};
  h.cards[0].abstract_type='unavailable';
  h.cards[0].unavailable_reason='No reading control';
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>{
    if(fn.name==='recentLinkDOM') return null;
    const value=await evaluate(fn,arg);
    if(value.url) value.url=aip.url;
    return value;
  };
  const result=await collectIssueBatch(h.tab,aip,{checkpointPath,saveHtml:true});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.reason,'recent_link_unavailable');
  assert.deepEqual(result.remaining_dois,['10.1021/a']);
});

test('CUA adapter resolves the documented cua.getTab signature and rejects fabricated legacy runtime',async t=>{
  const {collectCuaIssueBatch}=await import('../../src/papertrack/browser/cua.mjs');
  const checkpointPath=await fixture(t),h=harness(),bound=[];
  h.tab.close=async()=>{};
  const cua={getTab:async(...args)=>{bound.push(args);return h.tab;}};
  const result=await collectCuaIssueBatch(cua,'chrome','task-tab',request,{checkpointPath});
  assert.equal(result.status,'complete');
  assert.deepEqual(bound,[['task-tab',{browser:'chrome'}]]);
  const fabricated={browsers:{get:async id=>({browserId:id})}};
  await assert.rejects(collectCuaIssueBatch(fabricated,'chrome','task-tab',request,{checkpointPath}),/cua_browser_tabs_api_required/);
});

test('CUA adapter reuses a real bound tab across batches without getTab snapshots',async t=>{
  const {collectCuaIssueBatch}=await import('../../src/papertrack/browser/cua.mjs');
  const checkpointPath=await fixture(t),h=harness();
  h.tab.id='owned';h.tab.close=async()=>{};
  const cua={getTab:async()=>{assert.fail('Already bound tab must not be selected again');}};
  const first=await collectCuaIssueBatch(cua,'chrome','owned',request,{checkpointPath,tab:h.tab,maxInteractions:1});
  assert.equal(first.status,'partial');
  const second=await collectCuaIssueBatch(cua,'chrome','owned',request,{checkpointPath,tab:h.tab,maxInteractions:1});
  assert.equal(second.status,'complete');
  await assert.rejects(collectCuaIssueBatch(cua,'chrome','wrong-id',request,{checkpointPath,tab:h.tab}),/bound_tab_identity_mismatch/);
});

test('current In Progress issue refuses completion and saves separate evidence',async t=>{
  const checkpointPath=await fixture(t),h=harness();
  const original='{"prior":"preserve"}';
  await fs.writeFile(checkpointPath,original);
  const evaluate=h.tab.playwright.evaluate;
  h.tab.playwright.evaluate=async(fn,arg)=>{
    if(fn.name==='inspectSilverchairDOM') return {url:request.url,headings:['Volume 66, Issue 12'],cards:h.cards,
      issue_completion_markers:[{kind:'selected_option',url:request.url,text:'Volume 66, Issue 12 (In Progress)'}]};
    return evaluate(fn,arg);
  };
  const result=await collectIssueBatch(h.tab,request,{checkpointPath,saveHtml:false});
  assert.equal(result.status,'needs_attention');
  assert.equal(result.reason,'publisher_issue_incomplete');
  assert.equal(result.inventory_complete,false);
  assert.equal(await fs.readFile(checkpointPath,'utf8'),original);
  assert.equal(h.calls.length,0);
  await fs.access(checkpointPath+'.incomplete.html');
  const saved=JSON.parse(await fs.readFile(checkpointPath+'.incomplete.json','utf8'));
  assert.equal(saved.source_evidence[0].text,'Volume 66, Issue 12 (In Progress)');
});

for(const [value,text,expected] of [
  ['/jcisd8/issue/66/12','Volume 66, Issue 12 (In Progress)',1],
  ['/jcisd8/issue/66/12','Volume 66, Issue 12 (partial)',1],
  ['/jcisd8/issue/66/13','Volume 66, Issue 13 (In Progress)',0],
  ['', 'In Progress',0],
  ['/jcisd8/issue/66/12','Future date - Volume 66, Issue 12',0],
]) test('completion DOM marker is bound to current selected option: '+text,()=>{
  const doc=globalThis.document,loc=globalThis.location;
  try {
    globalThis.location={href:request.url};
    globalThis.document={querySelectorAll:selector=>selector.startsWith('option')?[{getAttribute:()=>value,innerText:text}]:[]};
    assert.equal(inspectSilverchairDOM().issue_completion_markers.length,expected);
  } finally {globalThis.document=doc;globalThis.location=loc;}
});

test('current incomplete heading is explicit at the access gate',async()=>{
  const tab={playwright:{evaluate:async()=>({url:request.url,title:'Issue',cards:2,
    headings:['Volume 66, Issue 12 (In Progress)'],challenge:false})}};
  const result=await probeIssueAccess(tab,request);
  assert.equal(result.status,'needs_attention');
  assert.equal(result.reason,'publisher_issue_incomplete');
  assert.equal(result.source_evidence[0].text,'Volume 66, Issue 12 (In Progress)');
});
