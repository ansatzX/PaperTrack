/** Adapter for real cua_repl handles; no browser transport or runtime fabrication. */
import {collectIssueBatch,collectRecentBatch} from './collect.mjs';
import {discoverLatestIssue} from './discovery.mjs';

async function resolveTab(runtime,browserId,tabId,boundTab=null) {
  if (!browserId || !tabId) throw Error('selected_browser_and_tab_required');
  let getTab;
  if (typeof runtime?.getTab==='function') {
    getTab=()=>runtime.getTab(tabId,{browser:browserId});
  } else if (typeof runtime?.browsers?.get==='function') {
    const browser=await runtime.browsers.get(browserId);
    if(typeof browser?.tabs?.get!=='function') throw Error('cua_browser_tabs_api_required: pass the real cua runtime or existing legacy browser binding; do not construct agent mocks');
    getTab=()=>browser.tabs.get(tabId);
  } else {
    throw Error('cua_browser_runtime_required: pass the real cua object from cua_repl');
  }
  if(boundTab && (boundTab.id===undefined || String(boundTab.id)!==String(tabId))) throw Error('bound_tab_identity_mismatch');
  const tab=boundTab || await getTab();
  if(typeof tab?.playwright?.evaluate!=='function') throw Error('dom_browser_tab_required');
  return tab;
}

export async function discoverCuaLatestIssue(agent,browserId,tabId,request,options) {
  const {tab:boundTab,...settings}=options || {};
  const tab=await resolveTab(agent,browserId,tabId,boundTab);
  return discoverLatestIssue(tab,request,settings);
}

export async function collectCuaIssueBatch(agent, browserId, tabId, request, options={}) {
  const {tab:boundTab,abstractTab:boundAbstractTab,abstractTabId,...settings}=options;
  const tab=await resolveTab(agent,browserId,tabId,boundTab);
  const detailId=abstractTabId || boundAbstractTab?.id;
  const abstractTab=detailId?await resolveTab(agent,browserId,detailId,boundAbstractTab):null;
  const result=await collectIssueBatch(tab,request,{...settings,abstractTab});
  if (result.status==='complete') {
    await tab.close();
    if (abstractTab) await abstractTab.close();
    result.task_tabs_closed=true;
  }
  return result;
}

export async function collectCuaRecentBatch(agent,browserId,tabId,request,options) {
  const {tab:boundTab,...settings}=options || {};
  const tab=await resolveTab(agent,browserId,tabId,boundTab);
  const result=await collectRecentBatch(tab,request,settings);
  if(result.status==='complete') {await tab.close();result.task_tabs_closed=true;}
  return result;
}

/** Save one archive page; Python owns dated inventory and traversal decisions. */
export async function collectCuaArchivePage(agent,browserId,tabId,request,{capturePath,tab:boundTab}) {
  const {saveIssueHtml}=await import('./html.mjs');
  const {writeFile,rename}=await import('node:fs/promises');
  const tab=await resolveTab(agent,browserId,tabId,boundTab);
  const observed=await tab.playwright.evaluate(function archiveIdentityDOM() {
    return {url:location.href,title:document.title,
      challenge:!!document.querySelector('#challenge-running, #challenge-form')};
  });
  const wanted=new URL(request.url),actual=new URL(observed.url);
  if (wanted.origin!==actual.origin) return {status:'needs_access',reason:'wrong_origin'};
  await saveIssueHtml(tab,capturePath+'.html');
  if (observed.challenge || /just a moment|access denied|attention required|verify you are human/i.test(observed.title))
    return {status:'needs_access',reason:'archive_blocked'};
  const data={schema:'papertrack-browser-archive-v1',requested_url:request.url,
    url:observed.url,captured_at:new Date().toISOString()};
  await writeFile(capturePath+'.tmp',JSON.stringify(data,null,2));
  await rename(capturePath+'.tmp',capturePath);
  return {status:'captured',capturePath};
}
