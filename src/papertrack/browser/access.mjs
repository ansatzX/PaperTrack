/** Read-only access gate. Never solves challenges or changes browser state. */
export function validateRequest(request) {
  const url = new URL(request.url);
  const volume = String(request.volume), issue = String(request.issue);
  if (url.protocol !== 'https:' || url.port || url.username || url.password ||
      !/^\d+$/.test(volume) || !/^\d+$/.test(issue) || Number(volume)<1 || Number(issue)<1) throw Error('invalid_issue_request');
  let match;
  if (url.hostname === 'pubs.acs.org') {
    match = url.pathname.match(/^\/([^/]+)\/issue\/(\d+)\/(\d+)\/?$/);
  } else if (url.hostname === 'pubs.aip.org') {
    match = url.pathname.match(/^\/(?:aip\/)?([^/]+)\/issue\/(\d+)\/(\d+)\/?$/);
  } else if (url.hostname === 'journals.aps.org') {
    match = url.pathname.match(/^\/([^/]+)\/issues\/(\d+)\/(\d+)\/?$/);
  }
  if (!match || match[2] !== volume || match[3] !== issue) throw Error('invalid_issue_request');
  return {host:url.hostname, code:match[1], volume, issue};
}

export async function probeIssueAccess(tab, request) {
  const expected = validateRequest(request);
  const observed = await tab.playwright.evaluate(function probeIssueDOM() {
    return {
      url:location.href, title:document.title,
      headings:Array.from(document.querySelectorAll('h1,h2,.volume-issue__wrap')).map(e => e.innerText.trim()),
      cards:document.querySelectorAll('.al-article-item-wrap').length || document.querySelectorAll('.al-article-items').length ||
        document.querySelectorAll('section.headline .title a[href*="/abstract/10."]').length,
      challenge:!!document.querySelector('#challenge-running, #challenge-form, iframe[src*="challenges.cloudflare.com"]'),
    };
  });
  const safe = {title:observed.title, card_count:observed.cards};
  if (/just a moment|attention required|access denied|verify you are human/i.test(observed.title) ||
      (observed.challenge && !observed.cards)) {
    return {status:'needs_access', reason:'publisher_challenge', ...safe};
  }
  let actual;
  try {actual = validateRequest({url:observed.url,volume:request.volume,issue:request.issue});}
  catch {return {status:'needs_access',reason:'not_on_requested_issue',...safe};}
  if (actual.host !== expected.host || actual.code !== expected.code) {
    return {status:'wrong_issue',reason:'different_journal',...safe};
  }
  const heading = `Volume ${expected.volume}, Issue ${expected.issue}`;
  if (expected.host === 'journals.aps.org' && observed.headings.includes(`${heading} (partial)`)) {
    return {status:'needs_attention',reason:'publisher_partial_issue',...safe};
  }
  const markedHeading=observed.headings.find(text=>
    new RegExp('^Volume\\s+'+expected.volume+'\\s*,?\\s*Issue\\s+'+expected.issue+'(?!\\d).*\\b(?:in progress|partial)\\b','i').test(text));
  if(markedHeading) return {status:'needs_attention',reason:'publisher_issue_incomplete',
    source_evidence:[{kind:'current_heading',url:observed.url,text:markedHeading}],...safe};
  if (!observed.headings.includes(heading)) {
    return {status:'needs_access',reason:'issue_heading_unavailable',...safe};
  }
  if (!observed.cards) return {status:'needs_extractor_update',reason:'article_cards_unrecognized',...safe};
  return {status:'ready',...safe};
}
