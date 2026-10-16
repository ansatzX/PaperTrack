import {captureApsIssue} from './aps_issue.mjs';

/** Capture an issue through a supplied, already connected browser-client tab.
 * No HTTP requests, profile reads, PDF/full-text visits, or browser launching.
 * The caller owns browser selection, authentication and tab finalization.
 */
export async function captureIssue(tab, request, {navigate = true, onProgress = () => {}, maxInteractions = 4, abstractTimeoutMs = 10000, previous = null, abstractTab = null} = {}) {
  const expected = new URL(request.url);
  if (expected.hostname === "journals.aps.org") {
    return captureApsIssue(tab, request, {abstractTab, navigate, previous, maxInteractions});
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
  const inspect = async () => tab.playwright.evaluate(() => ({
    url: location.href,
    headings: Array.from(document.querySelectorAll('h2, .volume-issue__wrap')).map(e => e.innerText.trim()),
    cards: Array.from(document.querySelectorAll('.al-article-items')).map(card => {
      const text = selector => card.querySelector(selector)?.innerText?.replace(/\s+/g, ' ').trim() || '';
      const control = card.querySelector('.showAbstractLink');
      const abstract = control && document.getElementById(control.getAttribute('aria-controls'));
      return {
        title: text('h3.item-title a'), article_url: card.querySelector('h3.item-title a')?.getAttribute('href') || '', doi: card.querySelector('.citation-label a')?.getAttribute('href') || '',
        authors: Array.from(card.querySelectorAll('.al-authors-list .wi-fullname')).map(e => e.innerText.trim()),
        citation: text('.ww-citation-primary'),
        abstract: abstract?.getAttribute('aria-hidden') === 'false'
          ? (control.getAttribute('data-abstract-type') === 'extract'
            ? Array.from(abstract.querySelectorAll('p')).map(e => {
                return e.innerHTML.replace(/<sup[^>]*>(.*?)<\/sup>/gi, '^{$1}')
                  .replace(/<sub[^>]*>(.*?)<\/sub>/gi, '_{$1}')
                  .replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
                  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
                  .replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();
              }).join('\n\n')
            : Array.from(abstract.querySelectorAll('section.abstract:not(.graphicalAbstract)')).map(e => e.innerText.trim()).join(' ')) : '',
        abstract_type: control?.getAttribute('data-abstract-type') || '',
        control: control?.id || '', expanded: control?.getAttribute('aria-expanded') === 'true',
      };
    }),
  }));
  let initial = await inspect();
  if (new URL(initial.url).hostname !== expected.hostname ||
      !initial.headings.includes(`Volume ${request.volume}, Issue ${request.issue}`) || !initial.cards.length) {
    throw new Error('Issue identity/content unavailable: inspect the browser for login, challenge, or changed layout');
  }
  const captured = new Map(initial.cards.filter(e => e.abstract).map(e => [e.doi, e.abstract]));
  if (previous) {
    if (previous.schema !== 'papertrack-browser-issue-v1' || previous.url !== initial.url ||
        previous.volume !== String(request.volume) || previous.issue !== String(request.issue) ||
        JSON.stringify(previous.entries.map(e => e.doi)) !== JSON.stringify(initial.cards.map(e => e.doi))) {
      throw new Error('Previous capture belongs to a different issue inventory');
    }
    for (const card of previous.entries) if (card.abstract) captured.set(card.doi, card.abstract);
  }
  // Silverchair opens floating abstract panels over subsequent article controls.
  // Capture each panel, then close it before moving to the next article.
  for (const card of initial.cards.filter(e => e.expanded)) {
    if (!/^[\w-]+$/.test(card.control)) throw new Error('Unexpected abstract control ID');
    await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
  }
  const errors = [];
  const turnDeadline = Date.now() + 30000;
  let visited = 0;
  for (const card of initial.cards) {
    // User policy: publisher Extracts fill the reading abstract field; retain their type.
    if (!['abstract', 'extract'].includes(card.abstract_type) || !card.control || captured.has(card.doi)) continue;
    if (visited >= maxInteractions || Date.now() >= turnDeadline) break;
    if (!/^[\w-]+$/.test(card.control)) throw new Error('Unexpected abstract control ID');
    try {
      await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
      const deadline = Date.now() + abstractTimeoutMs;
      let ready = false;
      while (Date.now() < deadline) {
        const state = await inspect();
        const observed = state.cards.find(e => e.doi === card.doi);
        ready = !!observed?.abstract;
        if (ready) captured.set(card.doi, observed.abstract);
        if (ready) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (!ready) errors.push({doi: card.doi, reason: 'abstract_not_loaded'});
      if (await tab.playwright.locator(`#${card.control}`).getAttribute('aria-expanded') === 'true') {
        await tab.playwright.locator(`#${card.control}`).click({timeoutMs: 5000});
      }
    } catch (error) {
      errors.push({doi: card.doi, reason: 'abstract_interaction_failed'});
    }
    onProgress({visited: ++visited, total: initial.cards.filter(e => ['abstract', 'extract'].includes(e.abstract_type)).length});
  }
  const final = await inspect();
  if (final.url !== initial.url || JSON.stringify(final.cards.map(e => e.doi)) !== JSON.stringify(initial.cards.map(e => e.doi))) {
    throw new Error('Issue membership changed during capture; no snapshot produced');
  }
  for (const card of final.cards) card.abstract = captured.get(card.doi) || card.abstract;
  return {schema: 'papertrack-browser-issue-v1', captured_at: new Date().toISOString(),
    requested_url: request.url, url: final.url, volume: String(request.volume), issue: String(request.issue),
    headings: final.headings, card_count: final.cards.length, entries: final.cards, errors,
    error_history: [...(previous?.error_history || previous?.errors || []), ...errors],
    remaining_abstracts: final.cards.filter(e => ['abstract', 'extract'].includes(e.abstract_type) && !e.abstract).length};
}
