import test from 'node:test';
import assert from 'node:assert/strict';
import {captureApsIssue} from './aps_issue.mjs';

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
      locator:()=>({waitFor:async()=>{}}),evaluate:async()=>({url,
        doi:wrongDoi?'DOI: https://doi.org/10.1103/other':`DOI: https://doi.org/${url.split('/abstract/')[1]}`,
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
});
test('APS mismatched abstract DOI is a gap rather than borrowed text',async()=>{
  const {tab,detail}=harness({wrongDoi:true});
  const result=await captureApsIssue(tab,request,{abstractTab:detail,maxInteractions:1});
  assert.equal(result.errors.length,1);
  assert.equal(result.remaining_abstracts,2);
  assert.equal(result.entries[0].abstract,'');
});
