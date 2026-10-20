import test from 'node:test';
import assert from 'node:assert/strict';
import {captureIssue} from './issue.mjs';

function harness({badIdentity = false, drift = false, neverLoads = []} = {}) {
  const calls = [];
  const ready = {first:()=>({waitFor:async()=>{}})};
  const cards = ['a','b','c','d','e'].map((id,i)=>({doi:`10.1021/${id}`,title:id,
    control:`aria${i}`,abstract_type:i===4?'extract':'abstract',abstract:'',expanded:false}));
  let reads = 0;
  return {calls, tab:{goto:async url=>calls.push(['goto',url]),playwright:{
    getByText:()=>ready,
    evaluate:async ()=>({url:'https://pubs.acs.org/jcisd8/issue/66/12',
      headings:badIdentity?['Wrong issue']:['Volume 66, Issue 12'],
      cards:structuredClone(drift && ++reads > 1? cards.slice(1):cards)}),
    locator:selector=>({first:ready.first,getAttribute:async()=>String(cards.find(c=>'#'+c.control===selector).expanded),click:async()=>{calls.push(['click',selector]);
      if(cards.some(c=>c.expanded && '#'+c.control!==selector)) throw new Error('Previous abstract overlay blocks this button');
      const card=cards.find(c=>'#'+c.control===selector);card.expanded=!card.expanded;card.abstract=card.expanded&&!neverLoads.includes(card.doi)?'Loaded text':'';}})
  }}};
}
const request={url:'https://pubs.acs.org/toc/jcisd8/66/12',volume:'66',issue:'12'};

test('bounded batches resume without revisiting loaded text and include editorial extracts',async()=>{
  const {tab,calls}=harness();
  const first=await captureIssue(tab,request,{maxInteractions:2});
  assert.equal(first.remaining_abstracts,3);
  const second=await captureIssue(tab,request,{navigate:false,maxInteractions:3,previous:first});
  assert.equal(second.remaining_abstracts,0);
  assert.equal(calls.filter(c=>c[0]==='goto').length,1);
  assert.deepEqual(calls.filter(c=>c[0]==='click').map(c=>c[1]),['#aria0','#aria0','#aria1','#aria1','#aria2','#aria2','#aria3','#aria3','#aria4','#aria4']);
});
test('wrong issue cannot be clicked or captured',async()=>{
  const {tab,calls}=harness({badIdentity:true});
  await assert.rejects(captureIssue(tab,request),/Issue identity/);
  assert.equal(calls.filter(c=>c[0]==='click').length,0);
});
test('resume does not relabel cached Extract text as Abstract',async()=>{
  const {tab}=harness();
  const first=await captureIssue(tab,request,{maxInteractions:1});
  first.entries[0].abstract_type='extract';
  first.entries[0].abstract='Old correction extract';
  const resumed=await captureIssue(tab,request,{navigate:false,maxInteractions:0,previous:first});
  assert.equal(resumed.entries[0].abstract_type,'abstract');
  assert.equal(resumed.entries[0].abstract,'');
  assert.equal(resumed.remaining_abstracts,5);
});
test('membership change fails the capture',async()=>{
  const {tab}=harness({drift:true});
  await assert.rejects(captureIssue(tab,request,{maxInteractions:0}),/membership changed/);
});

test('DOM parser prefers Abstract over an earlier Extract control',async()=>{
  const {inspectSilverchairDOM}=await import('../src/papertrack/browser/dom.mjs');
  const oldDocument=globalThis.document, oldLocation=globalThis.location;
  const control=kind=>({id:kind,innerText:kind,getAttribute:name=>name==='data-abstract-type'?kind:null});
  const card={querySelector:()=>null,querySelectorAll:selector=>selector==='.showAbstractLink'?[control('extract'),control('abstract')]:[]};
  try {
    globalThis.location={href:'https://pubs.acs.org/jcisd8/issue/66/12'};
    globalThis.document={querySelectorAll:selector=>selector==='.al-article-item-wrap'?[card]:[]};
    const result=inspectSilverchairDOM();
    assert.equal(result.cards[0].abstract_type,'abstract');
    assert.equal(result.cards[0].control,'abstract');
  } finally {
    if(oldDocument===undefined) delete globalThis.document; else globalThis.document=oldDocument;
    if(oldLocation===undefined) delete globalThis.location; else globalThis.location=oldLocation;
  }
});

test('recent collection targets only exact issue DOIs and only Abstract controls',async()=>{
  const {captureRecentPage}=await import('../src/papertrack/browser/issue.mjs');
  const {tab,calls}=harness();
  const evaluate=tab.playwright.evaluate;
  const url='https://pubs.acs.org/jcisd8/latest-articles';
  tab.playwright.evaluate=async()=>({...await evaluate(),url,headings:['Latest Articles']});
  const data=await captureRecentPage(tab,{...request,url,issueUrl:request.url},{targetDois:['10.1021/b','10.1021/e'],maxInteractions:4});
  assert.equal(data.schema,'papertrack-browser-recent-v1');
  assert.deepEqual(data.entries.map(e=>e.doi),['10.1021/b']);
  assert.deepEqual(calls,[['click','#aria1'],['click','#aria1']]);
  await assert.rejects(captureRecentPage(tab,{...request,url:'https://pubs.acs.org/jacsat/latest-articles',issueUrl:request.url},{}),/unsupported_recent_url/);
});


test('failed leading cards defer to untried cards while inventory retains official order',async()=>{
  const {tab,calls}=harness({neverLoads:['10.1021/a','10.1021/b']});
  const first=await captureIssue(tab,request,{maxInteractions:2,abstractTimeoutMs:1});
  assert.deepEqual(calls.filter(c=>c[0]==='click').map(c=>c[1]),['#aria0','#aria0','#aria1','#aria1']);
  assert.equal(first.remaining_abstracts,5);
  calls.length=0;
  const second=await captureIssue(tab,request,{navigate:false,previous:first,maxInteractions:2,abstractTimeoutMs:1});
  assert.deepEqual(calls.map(c=>c[1]),['#aria2','#aria2','#aria3','#aria3']);
  assert.equal(second.remaining_abstracts,3);
  assert.equal(second.errors.length,0);
  assert.equal(second.error_history.length,2);
  calls.length=0;
  const third=await captureIssue(tab,request,{navigate:false,previous:second,maxInteractions:1,abstractTimeoutMs:1});
  assert.deepEqual(calls.map(c=>c[1]),['#aria4','#aria4']);
  assert.deepEqual(third.entries.map(e=>e.doi),['10.1021/a','10.1021/b','10.1021/c','10.1021/d','10.1021/e']);
  assert.equal(third.remaining_abstracts,2);
  assert.equal(third.entries[0].abstract,'');
  assert.equal(third.entries[0].abstract_type,'abstract');
});

test('all failed cards remain retryable and expose repeated no-progress failures',async()=>{
  const {tab,calls}=harness({neverLoads:['a','b','c','d','e'].map(id=>'10.1021/'+id)});
  const first=await captureIssue(tab,request,{maxInteractions:5,abstractTimeoutMs:1});
  calls.length=0;
  const second=await captureIssue(tab,request,{navigate:false,previous:first,maxInteractions:5,abstractTimeoutMs:1});
  assert.equal(second.remaining_abstracts,first.remaining_abstracts);
  assert.equal(second.remaining_abstracts,5);
  assert.deepEqual(second.errors.map(e=>e.doi),first.errors.map(e=>e.doi));
  assert.equal(second.error_history.length,10);
  assert.equal(calls.length,10);
  assert.ok(second.entries.every(e=>!e.abstract));
});

test('failed missing-evidence cards defer without revisiting completed captures',async()=>{
  const {tab,calls}=harness();
  const first=await captureIssue(tab,request,{maxInteractions:2});
  first.errors=[{doi:'https://doi.org/10.1021/A',reason:'abstract_interaction_failed'}];
  first.error_history=first.errors;
  calls.length=0;
  const next=await captureIssue(tab,request,{navigate:false,previous:first,maxInteractions:1,
    readingMissing:new Set(['10.1021/a'])});
  assert.deepEqual(calls.map(c=>c[1]),['#aria2','#aria2']);
  assert.equal(next.entries[0].abstract,'Loaded text');
  assert.equal(next.entries[1].abstract,'Loaded text');
});

test('Extract supports outermost block-child-p and keeps nested prose once',async()=>{
  const {inspectSilverchairDOM}=await import('../src/papertrack/browser/dom.mjs');
  const oldDocument=globalThis.document,oldLocation=globalThis.location;
  const nested={innerHTML:'Nested',contains:()=>false};
  const outer={innerHTML:'Before <p>Nested</p> after <math><msup><mi>x</mi><mn>2</mn></msup></math>',contains:e=>e===nested};
  const last={innerHTML:'Last',contains:()=>false};
  const control={id:'c',innerText:'Extract',getAttribute:key=>({'data-abstract-type':'extract','aria-controls':'p','aria-expanded':'true'}[key])};
  const card={querySelector:()=>null,querySelectorAll:selector=>selector==='.showAbstractLink'?[control]:[]};
  const panel={getAttribute:()=> 'false',querySelectorAll:selector=>{
    assert.equal(selector,'p, div.block-child-p');return [outer,nested,last];
  }};
  try {
    globalThis.location={href:'https://pubs.aip.org/aip/jcp/issue/164/3'};
    globalThis.document={querySelectorAll:selector=>selector==='.al-article-item-wrap'?[card]:[],getElementById:()=>panel};
    const row=inspectSilverchairDOM().cards[0];
    assert.equal(row.abstract,'Before Nested after x2\n\nLast');
    assert.equal(row.abstract_type,'extract');
  } finally {globalThis.document=oldDocument;globalThis.location=oldLocation;}
});
