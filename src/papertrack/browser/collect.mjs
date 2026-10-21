/** Fixed collection entrypoint after agent-owned access. No LLM, network or browser launch. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID,createHash} from 'node:crypto';
import {probeIssueAccess,validateRequest} from './access.mjs';
import {captureIssue,captureRecentPage} from './issue.mjs';
import {saveIssueHtml} from './html.mjs';
import {readingEvidencePath,saveReadingEvidence,saveFailedReadingEvidence} from './reading.mjs';
export {readIssueHtml,saveIssueHtml} from './html.mjs';

const doiOf=value=>String(value).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase();
const needsAbstract=row=>row.abstract_type==='unavailable' &&
  !/(?:ctv|civ)\d{3}i\d{3}/i.test(row.doi) && !/editorial\s+masthead|issue\s+publication\s+information/i.test(row.title);

async function atomicWrite(file, value) {
  await fs.mkdir(path.dirname(file), {recursive:true});
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {await fs.writeFile(temporary,value,'utf8');await fs.rename(temporary,file);}
  finally {await fs.rm(temporary,{force:true});}
}

async function apsEvidenceValid(checkpoint,row,request) {
  const doi=doiOf(row.doi),key=createHash('sha256').update(doi).digest('hex');
  const base=path.join(checkpoint+'.articles',key);
  try {
    const [html,index]=await Promise.all([fs.readFile(base+'.html'),fs.readFile(base+'.json','utf8')]);
    const saved=JSON.parse(index),reading=saved.reading;
    const url=new URL(reading.url),issue=new URL(request.url);
    const observed=doiOf(String(reading.doi).replace(/^DOI:\s*/i,'').trim());
    if(!html.length || saved.schema!=='papertrack-aps-reading-v1' || saved.doi!==doi ||
      saved.sha256!==createHash('sha256').update(html).digest('hex') ||
      url.origin!==issue.origin || url.pathname!==`/${issue.pathname.split('/')[1]}/abstract/${doi}` ||
      observed!==doi || reading.mathPending) return false;
    if(row.abstract_type==='unavailable') return !!reading.unavailableArticle && !reading.abstract && !!row.unavailable_reason;
    const comparable=value=>String(value||'').normalize('NFKC').replace(/\s/g,'');
    return row.abstract_type==='abstract' && !!reading.abstract && comparable(reading.abstract)===comparable(row.abstract);
  } catch {return false;}
}

export async function collectIssueBatch(tab, request, {checkpointPath,abstractTab=null,
  maxInteractions=4,abstractTimeoutMs=10000,saveHtml=false}={}) {
  validateRequest(request);
  if (!checkpointPath || !path.isAbsolute(checkpointPath)) throw Error('absolute_checkpoint_path_required');
  if (!Number.isInteger(maxInteractions) || maxInteractions<1 || maxInteractions>8) throw Error('interaction_budget_must_be_1_to_8');
  if (!Number.isFinite(abstractTimeoutMs) || abstractTimeoutMs<1 || abstractTimeoutMs>15000) throw Error('invalid_abstract_timeout');
  let previous=null;
  try {previous=JSON.parse(await fs.readFile(checkpointPath,'utf8'));}
  catch(error) {if(error.code!=='ENOENT') throw error;}
  const access = await probeIssueAccess(tab,request);
  // Existing good checkpoints survive every later access failure.
  if (access.status!=='ready') {
    let evidence={};
    // Keep failed parser/access evidence separate from the last valid issue HTML.
    if (saveHtml || access.reason==='publisher_issue_incomplete') {
      try {evidence=await saveIssueHtml(tab,checkpointPath+'.access.html');}
      catch(error) {evidence={evidence_error:String(error.message).slice(0,500)};}
    }
    return {status:access.status,access,checkpoint:checkpointPath,...evidence};
  }
  const saveAbstract=saveHtml?async(detailTab,doi,{failed=false}={})=>{
    const key=createHash('sha256').update(doi.toLowerCase()).digest('hex');
    const evidence=await saveIssueHtml(detailTab,path.join(checkpointPath+'.articles',key+(failed?'.failed.html':'.html')),{apsAbstract:true});
    if(!failed) {
      const html=await fs.readFile(path.join(checkpointPath+'.articles',key+'.html'));
      await atomicWrite(path.join(checkpointPath+'.articles',key+'.json'),JSON.stringify({
        schema:'papertrack-aps-reading-v1',doi:doiOf(doi),
        sha256:createHash('sha256').update(html).digest('hex'),reading:evidence.reading})+'\n');
    }
    return evidence.reading;
  }:null;
  const readingMissing=new Set();
  if(saveHtml && previous) {
    for(const row of previous.entries) {
      if(new URL(request.url).hostname==='journals.aps.org') {
        if(!await apsEvidenceValid(checkpointPath,row,request)) readingMissing.add(doiOf(row.doi));
      } else {
        try {await fs.access(readingEvidencePath(checkpointPath,row.doi));}
        catch(error) {if(error.code!=='ENOENT') throw error;readingMissing.add(doiOf(row.doi));}
      }
    }
  }
  const saveReading=saveHtml?(source,row)=>saveReadingEvidence(source,checkpointPath,row):null;
  let data;
  try {
    data=await captureIssue(tab,request,{navigate:false,previous,abstractTab,maxInteractions,abstractTimeoutMs,saveAbstract,saveReading,readingMissing,
    saveFailure:saveHtml?(source,row,failure)=>saveFailedReadingEvidence(source,checkpointPath,row,failure):null});
  } catch(error) {
    if(error.message!=='publisher_issue_incomplete') throw error;
    const failure={status:'needs_attention',reason:'publisher_issue_incomplete',
      inventory_complete:false,url:request.url,source_evidence:error.sourceEvidence};
    try {Object.assign(failure,await saveIssueHtml(tab,checkpointPath+'.incomplete.html'));}
    catch(saveError) {failure.evidence_error=String(saveError.message).slice(0,500);}
    await atomicWrite(checkpointPath+'.incomplete.json',JSON.stringify(failure,null,2)+'\n');
    return failure;
  }
  if (previous) {
    const byDoi=new Map(data.entries.map(e=>[String(e.doi).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase(),e]));
    for(const old of previous.entries) {
      const next=byDoi.get(String(old.doi).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase());
      if (!next || (old.abstract && old.abstract!==next.abstract)) throw Error('checkpoint_content_regressed');
      for (const field of ['title','authors','citation','article_url']) {
        if (old[field]?.length && !next[field]?.length) throw Error('checkpoint_metadata_regressed');
      }
    }
  }
  data.capture_method='fixed_browser';
  data.collector='papertrack-browser-v2';
  data.inventory_complete=true; // Entire observed inventory rechecked by captureIssue; not a coverage oracle.
  const silverchair = ['pubs.acs.org','pubs.aip.org'].includes(new URL(request.url).hostname);
  if(silverchair && !data.errors.length && !data.remaining_abstracts && data.entries.some(needsAbstract)) {
    data.recent_url=await tab.playwright.evaluate(function recentLinkDOM({code}) {
      const link=Array.from(document.querySelectorAll('a[href]')).find(a=>{
        const url=new URL(a.getAttribute('href'),location.href);
        return url.origin===location.origin && url.pathname===`/${code}/latest-articles`;
      });
      return link?new URL(link.getAttribute('href'),location.href).href:null;
    },{code:validateRequest(request).code});
    if(!data.recent_url) {
      const failure={reason:'recent_link_unavailable',stage:'recent_discovery',url:data.url,
        target_dois:data.entries.filter(needsAbstract).map(row=>doiOf(row.doi)),
        expected_recent_path:`/${validateRequest(request).code}/latest-articles`,observed_recent_url:null};
      data.errors.push(failure);
      data.error_history.push(failure);
    }
  }
  await atomicWrite(checkpointPath,JSON.stringify(data,null,2)+'\n');
  // Avoid serializing the entire live DOM on every partial batch.
  if (saveHtml && (!previous || data.errors.length || !data.remaining_abstracts)) {
    await saveIssueHtml(tab,checkpointPath+'.html');
  }
  return {status:data.errors.length?'needs_attention':data.remaining_abstracts?'partial':data.recent_url?'needs_recent':'complete',
    recent_url:data.recent_url,
    ...(data.errors.some(error=>error.reason==='recent_link_unavailable')?{
      reason:'recent_link_unavailable',remaining_dois:data.entries.filter(needsAbstract).map(row=>doiOf(row.doi))}:{}),
    checkpoint:checkpointPath,total:data.entries.length,captured:data.entries.filter(e=>e.abstract).length,
    remaining:data.remaining_abstracts,unavailable:data.entries.filter(e=>!['abstract','extract'].includes(e.abstract_type)).map(e=>({doi:e.doi,reason:e.unavailable_reason})),
    errors:data.errors,collector:data.collector};
}

/** Supplement exact existing issue DOIs only; never create issue membership. */
export async function collectRecentBatch(tab,request,{checkpointPath,recentUrl,maxInteractions=4}={}) {
  validateRequest(request);
  if(!path.isAbsolute(checkpointPath) || !Number.isInteger(maxInteractions) || maxInteractions<1 || maxInteractions>8) throw Error('invalid_recent_options');
  const issue=JSON.parse(await fs.readFile(checkpointPath,'utf8'));
  if(issue.schema!=='papertrack-browser-issue-v1' || issue.inventory_complete!==true || issue.errors?.length || issue.remaining_abstracts!==0) throw Error('recent_requires_complete_issue_inventory');
  if(JSON.stringify(validateRequest({...request,url:issue.url}))!==JSON.stringify(validateRequest(request)) || !issue.recent_url) throw Error('recent_requires_formal_issue_checkpoint');
  const statePath=checkpointPath+'.recent.json';
  let state={schema:'papertrack-browser-recent-supplement-v1',issue_url:issue.url,root_url:issue.recent_url,pages:[],entries:[],complete:false};
  try {state=JSON.parse(await fs.readFile(statePath,'utf8'));} catch(error) {if(error.code!=='ENOENT') throw error;}
  if(state.schema!=='papertrack-browser-recent-supplement-v1' || state.issue_url!==issue.url || state.root_url!==issue.recent_url || !Array.isArray(state.pages) || !Array.isArray(state.entries)) throw Error('recent_checkpoint_identity_mismatch');
  const allowed=[issue.recent_url,...state.pages.map(p=>p.next_url).filter(Boolean)];
  if(!allowed.includes(recentUrl)) throw Error('recent_url_not_observed');
  if(state.pages.length>=20 && !state.pages.some(p=>p.url===recentUrl)) throw Error('recent_page_budget_exhausted');
  const targetDois=issue.entries.filter(needsAbstract).map(e=>doiOf(e.doi));
  const pageKey=createHash('sha256').update(recentUrl).digest('hex');
  const pagePath=path.join(checkpointPath+'.recent-pages',pageKey+'.json');
  const observed=await tab.playwright.evaluate(function recentPageIdentityDOM() {
    const next=document.querySelector('a[rel="next"]');
    return {url:location.href,title:document.title,challenge:!!document.querySelector('#challenge-running,#challenge-form'),
      next_url:next?new URL(next.getAttribute('href'),location.href).href:null};
  });
  if(observed.url!==recentUrl || observed.challenge || /just a moment|access denied|verify you are human/i.test(observed.title)) {
    await saveIssueHtml(tab,pagePath+'.access.html');
    return {status:'needs_access',recent_url:recentUrl};
  }
  let previous=null;
  try {previous=JSON.parse(await fs.readFile(pagePath,'utf8'));} catch(error) {if(error.code!=='ENOENT') throw error;}
  const data=await captureRecentPage(tab,{...request,url:recentUrl,issueUrl:issue.url},{previous,targetDois,maxInteractions,
    saveReading:(source,row)=>saveReadingEvidence(source,pagePath,row),
    saveFailure:(source,row,failure)=>saveFailedReadingEvidence(source,pagePath,row,failure)});
  await atomicWrite(pagePath,JSON.stringify(data,null,2)+'\n');
  if(!previous || data.errors.length || !data.remaining_abstracts) await saveIssueHtml(tab,pagePath+'.html');
  if(data.errors.length || data.remaining_abstracts) return {status:data.errors.length?'needs_attention':'partial',recent_url:recentUrl,remaining:data.remaining_abstracts,errors:data.errors};
  const byDoi=new Map(state.entries.map(row=>[doiOf(row.doi),row]));
  for(const row of data.entries.filter(e=>e.abstract_type==='abstract' && e.abstract)) byDoi.set(doiOf(row.doi),{...row,source_url:recentUrl});
  state.entries=[...byDoi.values()];
  state.pages=state.pages.filter(p=>p.url!==recentUrl);
  state.pages.push({url:recentUrl,next_url:observed.next_url});
  state.remaining_dois=targetDois.filter(doi=>!byDoi.has(doi));
  state.complete=state.remaining_dois.length===0;
  await atomicWrite(statePath,JSON.stringify(state,null,2)+'\n');
  return {status:state.complete?'complete':observed.next_url?'needs_recent':'needs_attention',
    recent_url:observed.next_url,remaining_dois:state.remaining_dois,
    reason:!state.complete&&!observed.next_url?'abstracts_not_found_on_observed_recent_pages':undefined};
}
