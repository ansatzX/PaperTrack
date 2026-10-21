import test from 'node:test';
import assert from 'node:assert/strict';
import {captureApsIssue} from '../../src/papertrack/browser/aps_issue.mjs';

const request={url:'https://journals.aps.org/prl/issues/136/1',volume:'136',issue:'1'};
function harness({wrongDoi=false}={}) {
  let url='';
  const rows=['a','b','a'].map(id=>({doi:`10.1103/${id}`,title:`Paper ${id}`,authors:['Author'],
    citation:'Phys. Rev. Lett. 136, 010101 (2026)',article_url:`/prl/abstract/10.1103/${id}`,
    abstract:'',abstract_type:'abstract'}));
  const calls=[];
  return {calls,tab:{goto:async()=>{},playwright:{locator:()=>({first:()=>({waitFor:async()=>{}})}),evaluate:async()=>({url:request.url,
    headings:['Volume 136, Issue 1'],entries:structuredClone(rows)})}},
    detail:{goto:async value=>{url=value;calls.push(value);},playwright:{
      locator:()=>({count:async()=>0,waitFor:async()=>{},first:()=>({waitFor:async()=>{},click:async()=>{}})}),evaluate:async()=>({url,
        doi:wrongDoi?'DOI: https://doi.org/10.1103/other':`DOI: https://doi.org/${new URL(url).pathname.split('/abstract/')[1]}`,
        abstract:'The full abstract, including mathematical symbol 𝛼.'})}}};
}
test('APS deduplicates highlighted entries and resumes only missing abstract pages',async()=>{
  const {tab,detail,calls}=harness();
  const first=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
  assert.equal(first.card_count,2);
  assert.equal(first.raw_card_count,3);
  assert.equal(first.remaining_abstracts,1);
  const final=await captureApsIssue(tab,request,{abstractTab:detail,previous:first,navigate:false});
  assert.equal(final.remaining_abstracts,0);
  assert.equal(calls.length,2);
  assert.ok(calls.every(url=>url.includes('/abstract/') && !url.includes('/pdf/')));
  assert.ok(calls.every(url=>new URL(url).hash==='#abstract-section-content'));
});

test('APS schedules new papers ahead of repeat failures and later recovers them',async()=>{
  const rows=['a','b','c'].map(id=>({doi:`10.1103/${id}`,title:`Paper ${id}`,authors:['Author'],
    citation:'Phys. Rev. Lett. 136, 010101 (2026)',article_url:`/prl/abstract/10.1103/${id}`,
    abstract:'',abstract_type:'abstract'}));
  const attempts=[];
  let currentUrl='';
  let aFails=true;
  const tab={goto:async()=>{},playwright:{locator:()=>({first:()=>({waitFor:async()=>{}})}),evaluate:async()=>({url:request.url,
    headings:['Volume 136, Issue 1'],entries:structuredClone(rows)})}};
  const detail={goto:async url=>{
    const doi=new URL(url).pathname.split('/abstract/')[1]; attempts.push(doi);
    currentUrl=url;
    if(doi==='10.1103/a' && aFails) {aFails=false;throw Error('temporary navigation failure');}
  },playwright:{
    locator:()=>({count:async()=>0,waitFor:async()=>{},first:()=>({click:async()=>{},waitFor:async()=>{}})}),
    evaluate:async()=>({url:currentUrl,
      doi:`DOI: https://doi.org/${new URL(currentUrl).pathname.split('/abstract/')[1]}`,
      abstract:'Full abstract'})
  }};

  const first=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
  assert.deepEqual(attempts,['10.1103/a']);
  assert.equal(first.errors[0].doi,'10.1103/a');
  assert.deepEqual(first.entries.map(e=>e.doi),rows.map(e=>e.doi));

  const second=await captureApsIssue(tab,request,{abstractTab:detail,previous:first,maxInteractions:1,navigate:false});
  assert.deepEqual(attempts,['10.1103/a','10.1103/b']);
  assert.equal(second.entries[1].abstract,'Full abstract');
  assert.deepEqual(second.entries.map(e=>e.doi),rows.map(e=>e.doi));

  const third=await captureApsIssue(tab,request,{abstractTab:detail,previous:second,maxInteractions:1,navigate:false});
  assert.deepEqual(attempts,['10.1103/a','10.1103/b','10.1103/c']);
  assert.equal(third.entries[1].abstract,'Full abstract');
  assert.equal(third.entries[2].abstract,'Full abstract');
  assert.deepEqual(third.entries.map(e=>e.doi),rows.map(e=>e.doi));

  const fourth=await captureApsIssue(tab,request,{abstractTab:detail,previous:third,maxInteractions:1,navigate:false});
  assert.deepEqual(attempts,['10.1103/a','10.1103/b','10.1103/c','10.1103/a']);
  assert.equal(fourth.entries[0].abstract,'Full abstract');
  assert.equal(fourth.entries[1].abstract,'Full abstract');
  assert.equal(fourth.entries[2].abstract,'Full abstract');
  assert.deepEqual(fourth.entries.map(e=>e.doi),rows.map(e=>e.doi));
  assert.equal(fourth.errors.length,0);
  assert.equal(fourth.error_history.filter(e=>e.reason==='abstract_visit_failed').length,1);

  const currentErrorsOnly=structuredClone(first);
  currentErrorsOnly.error_history=[];
  const emptyHistoryBatch=await captureApsIssue(tab,request,{abstractTab:detail,
    previous:currentErrorsOnly,maxInteractions:1,navigate:false});
  assert.deepEqual(attempts,['10.1103/a','10.1103/b','10.1103/c','10.1103/a','10.1103/b']);
  assert.equal(emptyHistoryBatch.entries[1].abstract,'Full abstract');
  assert.deepEqual(emptyHistoryBatch.entries.map(e=>e.doi),rows.map(e=>e.doi));
});

test('APS retains incomplete math evidence after a bounded rendering timeout',async()=>{
  const {tab,detail}=harness();
  detail.playwright.locator=()=>({count:async()=>0,waitFor:async()=>{},first:()=>({click:async()=>{},waitFor:async()=>{
    const error=new Error('render timed out');error.name='TimeoutError';throw error;
  }})});
  let saved=false;
  const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
    saveAbstract:async(source,doi)=>{saved=true;return {
      url:`https://journals.aps.org/prl/abstract/${doi}#abstract-section-content`,
      doi,abstract:'Text with a missing formula',mathPending:true};}});
  assert.equal(saved,true);
  assert.equal(result.entries[0].abstract,'');
  assert.match(result.errors[0].message,/math is not rendered/);
});
test('APS mismatched abstract DOI is a gap rather than borrowed text',async()=>{
  const {tab,detail}=harness({wrongDoi:true});
  const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
  assert.equal(result.errors.length,1);
  assert.equal(result.remaining_abstracts,2);
  assert.equal(result.entries[0].abstract,'');
});

test('APS navigation timeout accepts only a correctly loaded matching page',async()=>{
  for(const wrongDoi of [false,true]) {
    const {tab,detail}=harness({wrongDoi});
    const goto=detail.goto;
    detail.goto=async url=>{await goto(url);throw Error('Timed out waiting for CDP command Page.navigate.');};
    const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
    assert.equal(Boolean(result.entries[0].abstract),!wrongDoi);
    assert.equal(result.errors.length,wrongDoi?1:0);
    assert.equal(result.error_history[0].reason,'navigation_timeout_observed');
  }
});

test('APS failed page evidence is retained without accepting its text',async()=>{
  const {tab,detail}=harness();
  detail.goto=async()=>{throw Error('Navigation denied');};
  const saves=[];
  const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
    saveAbstract:async(source,doi,options)=>{saves.push({doi,...options});return {abstract:'Wrong page'};}});
  assert.deepEqual(saves,[{doi:'10.1103/a',failed:true}]);
  assert.equal(result.entries[0].abstract,'');
  assert.equal(result.errors[0].stage,'navigation');
});
test('APS resume cannot promote an Extract checkpoint to Abstract',async()=>{
  const {tab,detail}=harness();
  const previous=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
  previous.entries[0].abstract_type='extract';
  previous.entries[0].abstract='Correction text, not an abstract';
  const result=await captureApsIssue(tab,request,{previous,navigate:false,maxInteractions:0});
  assert.equal(result.entries[0].abstract,'');
  assert.equal(result.entries[0].abstract_type,'abstract');
  assert.equal(result.remaining_abstracts,2);
});

test('APS records abstract only after source HTML was retained',async()=>{
  const {tab,detail}=harness();
  const saved=[];
  const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
    saveAbstract:async(source,doi)=>{assert.equal(source,detail);saved.push(doi);
      return {url:`https://journals.aps.org/prl/abstract/${doi}`,doi,abstract:'Source text'};}});
  assert.deepEqual(saved,['10.1103/a']);
  assert.ok(result.entries[0].abstract);
  const failed=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
    saveAbstract:async()=>{throw Error('disk write failed');}});
  assert.equal(failed.entries[0].abstract,'');
  assert.equal(failed.errors[0].stage,'abstract_evidence');
});

test('APS does not silently count an unread erratum as complete',async()=>{
  const {tab}=harness();
  const evaluate=tab.playwright.evaluate;
  tab.playwright.evaluate=async()=>{
    const state=await evaluate();
    state.entries=state.entries.map(e=>({...e,title:'Erratum: Earlier paper'}));
    return state;
  };
  const result=await captureApsIssue(tab,request,{navigate:false});
  assert.equal(result.remaining_abstracts,2);
  assert.equal(result.entries.length,2);
});

test('APS retains source-proven no-abstract erratum without copying article text',async()=>{
  for(const wrongDoi of [false,true]) {
    const {tab,detail}=harness();
    detail.playwright.evaluate=async()=>({unavailableArticle:true});
    const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
      saveAbstract:async()=>({url:'https://journals.aps.org/prl/abstract/10.1103/a#abstract-section-content',
        doi:wrongDoi?'10.1103/other':'10.1103/a',abstract:'',unavailableArticle:true})});
    assert.equal(result.entries[0].abstract,'');
    assert.equal(result.entries[0].abstract_type,wrongDoi?'abstract':'unavailable');
    assert.equal(result.remaining_abstracts,wrongDoi?2:1);
    assert.equal(result.errors.length,wrongDoi?1:0);
  }
});

test('APS rechecks delayed erratum text but still requires matching saved evidence',async()=>{
  for(const ready of [false,true]) {
    const {tab,detail}=harness();
    let observations=0;
    detail.playwright.evaluate=async()=>({unavailableArticle:++observations>1 && ready});
    detail.playwright.locator=()=>({waitFor:async()=>{throw Error('selector deadline exceeded');}});
    const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1,
      saveAbstract:async()=>({url:'https://journals.aps.org/prl/abstract/10.1103/a',
        doi:'10.1103/a',abstract:'',unavailableArticle:ready})});
    assert.equal(observations,2);
    assert.equal(result.entries[0].abstract,'');
    assert.equal(result.entries[0].abstract_type,ready?'unavailable':'abstract');
    assert.equal(result.errors.length,ready?0:1);
  }
});

test('APS scrolls using only the same-page anchor, never clickable abstract prose',async()=>{
  for(const present of [false,true]) {
    const {tab,detail}=harness();
    const clicked=[];
    detail.playwright.locator=selector=>({
      count:async()=>present?1:0,waitFor:async()=>{},
      first:()=>({waitFor:async()=>{},click:async()=>{
        clicked.push(selector);
        assert.equal(selector,'a[href="#abstract-section-content"]');
      }})});
    const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
    assert.equal(result.errors.length,0);
    assert.equal(clicked.length,present?1:0);
  }
});
