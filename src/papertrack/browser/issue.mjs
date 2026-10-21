import {captureApsIssue} from './aps_issue.mjs';
import {inspectSilverchairDOM} from './dom.mjs';
import {validateRequest} from './access.mjs';

const doiOf = value => String(value).replace(/^https?:\/\/(?:dx\.)?doi\.org\//i,'').toLowerCase();
const inventoryKey = cards => JSON.stringify(cards.map(e=>doiOf(e.doi)).sort());

/** Capture an issue through a supplied, already connected browser-client tab.
 * No HTTP requests, profile reads, PDF/full-text visits, or browser launching.
 * The caller owns browser selection, authentication and tab finalization.
 */
export async function captureIssue(tab, request, {navigate = true, onProgress = () => {}, maxInteractions = 4, abstractTimeoutMs = 10000, previous = null, abstractTab = null, saveAbstract = null, saveReading = null, saveFailure = null, readingMissing = new Set()} = {}) {
  return captureSilverchairOrIssue(tab,request,{navigate,onProgress,maxInteractions,abstractTimeoutMs,previous,abstractTab,saveAbstract,saveReading,saveFailure,readingMissing});
}

export async function captureRecentPage(tab,request,options) {
  const formal=validateRequest({url:request.issueUrl,volume:request.volume,issue:request.issue});
  const actual=new URL(request.url),issue=new URL(request.issueUrl);
  if(actual.origin!==issue.origin || actual.pathname!==`/${formal.code}/latest-articles`) throw Error('unsupported_recent_url');
  return captureSilverchairOrIssue(tab,request,{...options,navigate:false,recent:true});
}

async function captureSilverchairOrIssue(tab, request, {navigate = true, onProgress = () => {}, maxInteractions = 4, abstractTimeoutMs = 10000, previous = null, abstractTab = null, saveAbstract = null, saveReading = null, saveFailure = null, readingMissing = new Set(), recent=false, targetDois=[]} = {}) {
  if(!recent)
  validateRequest(request);
  const expected = new URL(request.url);
  if (expected.hostname === "journals.aps.org") {
    return captureApsIssue(tab, request, {abstractTab, navigate, previous, maxInteractions, saveAbstract,readingMissing});
  }
  if (!['pubs.acs.org', 'pubs.aip.org'].includes(expected.hostname)) {
    throw new Error('This collector currently supports Silverchair ACS/AIP issue pages');
  }
  if (!request.volume || !request.issue) throw new Error('Formal volume and issue required');
  if (navigate) {
    await tab.goto(request.url);
    await tab.playwright.getByText(`Volume ${request.volume}, Issue ${request.issue}`, {exact:true}).first().waitFor({state:"visible", timeoutMs:15000});
    await tab.playwright.locator(".al-article-items").first().waitFor({state:"visible", timeoutMs:15000});
  }
  const targets=new Set(targetDois.map(doiOf));
  const inspect = async () => {
    const state=await tab.playwright.evaluate(inspectSilverchairDOM);
    if(!recent && state.issue_completion_markers?.length) {
      const error=new Error('publisher_issue_incomplete');
      error.sourceEvidence=state.issue_completion_markers;
      throw error;
    }
    if(recent) state.cards=state.cards.filter(e=>targets.has(doiOf(e.doi)) && e.abstract_type==='abstract');
    return state;
  };
  let initial = await inspect();
  if (new URL(initial.url).hostname !== expected.hostname ||
      (recent ? initial.url!==request.url : !initial.headings.includes(`Volume ${request.volume}, Issue ${request.issue}`) || !initial.cards.length)) {
    throw new Error('Issue identity/content unavailable: inspect the browser for login, challenge, or changed layout');
  }
  const ids = initial.cards.map(e=>doiOf(e.doi));
  if (new Set(ids).size !== ids.length || initial.cards.some(e=>!e.title || !/^10\.\d{4,9}\/\S+$/.test(doiOf(e.doi)))) {
    throw Error('Invalid or duplicate official article identities');
  }
  if (initial.cards.some(e=>!['abstract','extract','unavailable',''].includes(e.abstract_type))) {
    throw Error('Unknown publisher abstract control type');
  }
  if(!recent) {
    const actual = validateRequest({url:initial.url,volume:request.volume,issue:request.issue});
    const wanted = validateRequest(request);
    if (actual.host !== wanted.host || actual.code !== wanted.code) throw Error('Issue identity/content unavailable');
  }
  const captured = new Map(initial.cards.filter(e => e.abstract).map(e => [doiOf(e.doi), e.abstract]));
  if (previous) {
    if (previous.schema !== (recent?'papertrack-browser-recent-v1':'papertrack-browser-issue-v1') || new URL(previous.url).origin + new URL(previous.url).pathname !== new URL(initial.url).origin + new URL(initial.url).pathname ||
        previous.volume !== String(request.volume) || previous.issue !== String(request.issue) ||
        inventoryKey(previous.entries) !== inventoryKey(initial.cards)) {
      throw new Error('Previous capture belongs to a different issue inventory');
    }
    for (const card of previous.entries) {
      const current = initial.cards.find(e => doiOf(e.doi) === doiOf(card.doi));
      // A cached Extract must never be relabelled as an Abstract on resume.
      // Fresh visible source text takes precedence over a checkpoint.
      if (card.abstract && current.abstract_type === card.abstract_type && !captured.has(doiOf(card.doi))) {
        captured.set(doiOf(card.doi), card.abstract);
      }
    }
  }
  // Silverchair opens floating abstract panels over subsequent article controls.
  // Capture each panel, then close it before moving to the next article.
  for (const card of initial.cards.filter(e => e.expanded)) {
    if (card.abstract && saveReading) {await saveReading(tab,card);readingMissing.delete(doiOf(card.doi));}
    if (!/^[\w-]+$/.test(card.control)) throw new Error('Unexpected abstract control ID');
    await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
  }
  const errors = [];
  const recordFailure = async (card, failure) => {
    errors.push(failure);
    if (saveFailure) {
      try {failure.evidence = await saveFailure(tab,card,failure);}
      catch (error) {failure.evidence_error = String(error.message || error).slice(0,500);}
    }
  };
  const turnDeadline = Date.now() + 30000;
  let visited = 0;
  // Defer known failures within this already validated checkpoint inventory so
  // early stalled cards cannot consume every batch before later cards are read.
  // Keep the publisher inventory untouched; only interaction order changes.
  const failedDois = new Set([...(previous?.error_history || []), ...(previous?.errors || [])]
    .filter(error => ['abstract_not_loaded', 'abstract_interaction_failed'].includes(error.reason))
    .map(error => doiOf(error.doi)));
  // Publisher Extracts retain their type and participate like Abstracts.
  const pending = initial.cards.filter(card => ['abstract', 'extract'].includes(card.abstract_type)
    && (!captured.has(doiOf(card.doi)) || readingMissing.has(doiOf(card.doi))));
  const interactionOrder = [
    ...pending.filter(card => !failedDois.has(doiOf(card.doi))),
    ...pending.filter(card => failedDois.has(doiOf(card.doi)))
  ];
  for (const card of interactionOrder) {
    if (visited >= maxInteractions || Date.now() >= turnDeadline) break;
    if (!/^[\w-]+$/.test(card.control)) throw new Error('Unexpected abstract control ID');
    let stage = 'open';
    try {
      await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
      stage = 'read';
      const deadline = Date.now() + abstractTimeoutMs;
      let ready = false;
      while (Date.now() < deadline) {
        const state = await inspect();
        const observed = state.cards.find(e => e.doi === card.doi);
        ready = !!observed?.abstract;
        if (ready) {
          if (saveReading) await saveReading(tab,observed);
          readingMissing.delete(doiOf(card.doi));
          captured.set(doiOf(card.doi), observed.abstract);
        }
        if (ready) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (!ready) await recordFailure(card,{doi: card.doi, reason: 'abstract_not_loaded', stage});
      stage = 'close';
      if (await tab.playwright.locator(`#${card.control}`).getAttribute('aria-expanded') === 'true') {
        await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
      }
    } catch (error) {
      await recordFailure(card,{doi: card.doi, reason: 'abstract_interaction_failed', stage,
        exception:error.name,message:String(error.message || '').slice(0,500)});
      // A possibly open overlay can obstruct every later card; recover before continuing.
      break;
    }
    onProgress({visited: ++visited, total: initial.cards.filter(e => ['abstract', 'extract'].includes(e.abstract_type)).length});
  }
  const final = await inspect();
  if (final.cards.some(card => initial.cards.find(e => doiOf(e.doi) === doiOf(card.doi))?.abstract_type !== card.abstract_type)) {
    throw new Error('Abstract control type changed during capture; no snapshot produced');
  }
  if (new URL(final.url).origin + new URL(final.url).pathname !== new URL(initial.url).origin + new URL(initial.url).pathname || inventoryKey(final.cards) !== inventoryKey(initial.cards)) {
    throw new Error('Issue membership changed during capture; no snapshot produced');
  }
  for (const card of final.cards) card.abstract = captured.get(doiOf(card.doi)) || (saveReading?'':card.abstract);
  return {schema: recent?'papertrack-browser-recent-v1':'papertrack-browser-issue-v1', captured_at: new Date().toISOString(),
    requested_url: request.url, url: final.url, volume: String(request.volume), issue: String(request.issue),
    headings: final.headings, card_count: final.cards.length, entries: final.cards, errors,
    error_history: [...(previous?.error_history || previous?.errors || []), ...errors],
    remaining_abstracts: final.cards.filter(e => ['abstract', 'extract'].includes(e.abstract_type) && (!e.abstract || readingMissing.has(doiOf(e.doi)))).length};
}
