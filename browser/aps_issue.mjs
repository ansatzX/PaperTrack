/** APS issue inventory plus abstract-only detail visits in a separate tab. */
export async function captureApsIssue(tab, request, {abstractTab, navigate=true, previous=null, maxInteractions=4}={}) {
  const expected = new URL(request.url);
  if (expected.origin !== 'https://journals.aps.org' || !/^\/\w+\/issues\/\d+\/\d+$/.test(expected.pathname)) {
    throw new Error('Expected an APS formal issue URL');
  }
  if (navigate) {
    await tab.goto(request.url);
    await tab.playwright.locator("section.headline .title a").first().waitFor({state:"visible",timeoutMs:15000});
  }
  const inspect = () => tab.playwright.evaluate(() => ({url:location.href,
    headings:Array.from(document.querySelectorAll('main h1')).map(e=>e.textContent.trim()),
    entries:Array.from(document.querySelectorAll('section.headline .title a[href*="/abstract/10."]')).map(a=>{
      const card=a.closest('.content');
      const citation=card.querySelectorAll('.citation-container p');
      return {title:a.textContent.trim(),article_url:a.getAttribute('href'),
        doi:a.getAttribute('href').split('/abstract/')[1],
        authors:[citation[0]?.textContent.trim() || ''].filter(Boolean),
        citation:citation[1]?.textContent.trim() || '',abstract:'',abstract_type:'abstract'};
    })}));
  const initial=await inspect();
  if (initial.url !== request.url || !initial.headings.includes(`Volume ${request.volume}, Issue ${request.issue}`) || !initial.entries.length) {
    throw new Error('APS issue identity/content unavailable');
  }
  // Highlighted papers appear again in the sectioned contents; one DOI is one member.
  const entries=Array.from(new Map(initial.entries.map(e=>[e.doi.toLowerCase(),e])).values());
  if (previous) {
    if (previous.url!==initial.url || previous.schema!=='papertrack-browser-issue-v1' ||
        JSON.stringify(previous.entries.map(e=>e.doi))!==JSON.stringify(entries.map(e=>e.doi))) {
      throw new Error('Previous APS capture belongs to a different issue inventory');
    }
    for(let i=0;i<entries.length;i++) entries[i].abstract=previous.entries[i].abstract || '';
  }
  const errors=[];
  const deadline=Date.now()+30000;
  let visits=0;
  if(abstractTab) for(const entry of entries) {
    if(entry.abstract || /^erratum\b/i.test(entry.title)) continue;
    if(visits>=maxInteractions || Date.now()>=deadline) break;
    const detailUrl=new URL(entry.article_url,initial.url);
    if(detailUrl.origin!==expected.origin || !detailUrl.pathname.includes('/abstract/10.1103/')) {
      throw new Error('Unexpected APS abstract link');
    }
    visits++;
    let stage="navigation";
    try {
      await abstractTab.goto(detailUrl.href);
      stage="abstract_visibility";
      await abstractTab.playwright.locator('#abstract-section-content').waitFor({state:'visible',timeoutMs:10000});
      stage="abstract_identity";
      const detail=await abstractTab.playwright.evaluate(()=>({url:location.href,
        doi:Array.from(document.querySelectorAll('main p')).map(e=>e.textContent.trim()).find(t=>t.startsWith('DOI:')) || '',
        abstract:document.querySelector('#abstract-section-content')?.textContent.trim() || ''}));
      // textContent preserves rendered MathJax symbols which innerText can omit.
      if(detail.url!==detailUrl.href || !detail.doi.toLowerCase().endsWith(entry.doi.toLowerCase()) || !detail.abstract) {
        throw new Error('APS abstract identity/content missing');
      }
      entry.abstract=detail.abstract;
    } catch(error) { errors.push({doi:entry.doi,reason:'abstract_visit_failed',stage,exception:error.name}); }
  }
  const final=await inspect();
  if(final.url!==initial.url || JSON.stringify(final.entries.map(e=>e.doi))!==JSON.stringify(initial.entries.map(e=>e.doi))) {
    throw new Error('APS inventory changed during abstract visits');
  }
  return {schema:'papertrack-browser-issue-v1',captured_at:new Date().toISOString(),requested_url:request.url,
    url:initial.url,volume:String(request.volume),issue:String(request.issue),headings:initial.headings,
    card_count:entries.length,raw_card_count:initial.entries.length,entries,errors,error_history:[...(previous?.error_history || previous?.errors || []),...errors],
    remaining_abstracts:entries.filter(e=>!e.abstract && !/^erratum\b/i.test(e.title)).length};
}
