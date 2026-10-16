import test from 'node:test';
import assert from 'node:assert/strict';
import {captureIssue} from './issue.mjs';

function harness({badIdentity = false, drift = false} = {}) {
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
      const card=cards.find(c=>'#'+c.control===selector);card.expanded=!card.expanded;card.abstract=card.expanded?'Loaded text':'';}})
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
test('membership change fails the capture',async()=>{
  const {tab}=harness({drift:true});
  await assert.rejects(captureIssue(tab,request,{maxInteractions:0}),/membership changed/);
});
