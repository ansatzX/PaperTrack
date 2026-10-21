/** APS issue inventory plus abstract-only detail visits in a separate tab. */
export async function captureApsIssue(tab, request, {abstractTab, navigate=true, previous=null, maxInteractions=4, saveAbstract=null,readingMissing=new Set()}={}) {
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
    for(let i=0;i<entries.length;i++) {
      if(readingMissing.has(entries[i].doi.toLowerCase())) continue;
      if(previous.entries[i].abstract_type === 'abstract') {
        entries[i].abstract=previous.entries[i].abstract || '';
      } else if(previous.entries[i].abstract_type === 'unavailable' && previous.entries[i].unavailable_reason) {
        entries[i].abstract_type='unavailable';
        entries[i].unavailable_reason=previous.entries[i].unavailable_reason;
      }
    }
  }
  const errors=[];
  const navigationWarnings=[];
  const deadline=Date.now()+30000;
  let visits=0;
  // A historical visit failure stays visible, but should not consume every
  // bounded batch ahead of papers that have not yet had a visit attempt.
  // Keep `entries` in official issue order; only schedule the visits differently.
  const failedDois=new Set([...(previous?.error_history || []),...(previous?.errors || [])]
    .filter(error=>error.reason==='abstract_visit_failed')
    .map(error=>String(error.doi).replace(/^https?:\/\/(?:dx\.)?doi\.org\//i,'').toLowerCase()));
  const visitOrder=[
    ...entries.filter(entry=>!failedDois.has(String(entry.doi).toLowerCase())),
    ...entries.filter(entry=>failedDois.has(String(entry.doi).toLowerCase()))
  ];
  if(abstractTab) for(const entry of visitOrder) {
    if(entry.abstract || entry.abstract_type==='unavailable') continue;
    if(visits>=maxInteractions || Date.now()>=deadline) break;
    const detailUrl=new URL(entry.article_url,initial.url);
    if(detailUrl.origin!==expected.origin || !detailUrl.pathname.includes('/abstract/10.1103/')) {
      throw new Error('Unexpected APS abstract link');
    }
    visits++;
    let stage="navigation";
    try {
      // Bring the actual abstract into the viewport. APS defers MathJax outside
      // the viewport, so container visibility alone does not mean math is ready.
      const readingUrl=new URL(detailUrl.href);
      readingUrl.hash='abstract-section-content';
      try {
        await abstractTab.goto(readingUrl.href);
      } catch(error) {
        if (!/timed out|timeout/i.test(String(error.message))) throw error;
        // A navigation acknowledgement timeout does not prove the page failed
        // to load. Observe the rendered page below without another navigation;
        // exact URL, DOI, abstract and saved HTML gates still apply.
        navigationWarnings.push({doi:entry.doi,stage:'navigation',
          reason:'navigation_timeout_observed',message:String(error.message).slice(0,500)});
      }
      const inspectNoAbstract=()=>abstractTab.playwright.evaluate(()=>({
        url:location.href,
        doi:Array.from(document.querySelectorAll('main p')).map(p=>p.textContent.trim()).find(t=>t.startsWith('DOI:')) || '',
        unavailableArticle:!!document.querySelector('#article-page-wrapper[data-article-type="erratum"] #fulltext-content, #article-page-wrapper[data-article-type="comment"] #fulltext-content, #article-page-wrapper[data-article-type="reply"] #fulltext-content, #article-page-wrapper[data-article-type="retraction"] #fulltext-content')?.textContent.trim()
          && !document.querySelector('#abstract-section-content, #section-abstract, a[href="#abstract-section-content"]')
      }));
      let noAbstract=await inspectNoAbstract();
      if(!noAbstract.unavailableArticle) {
        stage='abstract_visibility';
        try {
          await abstractTab.playwright.locator('#abstract-section-content').waitFor({state:'visible',timeoutMs:10000});
        } catch(error) {
          // Article Text may finish loading after the first DOM observation.
          // Re-observe once; a timeout alone is never evidence of no abstract.
          noAbstract=await inspectNoAbstract();
          if(!noAbstract.unavailableArticle) throw error;
        }
      }
      if(noAbstract.unavailableArticle) {
        stage='unavailable_evidence';
        if(!saveAbstract) throw Error('APS unavailable abstract requires saved HTML');
        const evidence=await saveAbstract(abstractTab,entry.doi);
        if(!evidence?.unavailableArticle || ![detailUrl.href,readingUrl.href].includes(evidence.url) ||
           !evidence.doi.toLowerCase().endsWith(entry.doi.toLowerCase()) || evidence.abstract) {
          throw Error('APS unavailable abstract identity/evidence mismatch');
        }
        entry.abstract_type='unavailable';
        entry.unavailable_reason='Official article page contains Article Text but no Abstract section or control';
        continue;
      }
      stage="abstract_visibility";
      // Fragment scrolling can run before APS finishes laying out the page.
      // Use only the same-page Abstract anchor. Clicking the paragraph center
      // can activate a citation link and navigate to an unrelated paper.
      const abstractAnchor=abstractTab.playwright.locator('a[href="#abstract-section-content"]');
      if(await abstractAnchor.count()) await abstractAnchor.first().click({timeoutMs:5000});
      // A timeout still proceeds to the evidence snapshot, which records the
      // incomplete DOM and is rejected by the mathPending gate below.
      try {
        await abstractTab.playwright.locator('#abstract-section-content > p mjx-lazy').first().waitFor({state:'detached',timeoutMs:5000});
      } catch(error) {
        if(error.name!=='TimeoutError' && !/timeout|timed out/i.test(String(error.message))) throw error;
      }
      stage=saveAbstract?'abstract_evidence':'abstract_identity';
      const detail=saveAbstract?await saveAbstract(abstractTab,entry.doi):await abstractTab.playwright.evaluate(()=>({url:location.href,
        doi:Array.from(document.querySelectorAll('main p')).map(e=>e.textContent.trim()).find(t=>t.startsWith('DOI:')) || '',
        abstract:Array.from(document.querySelectorAll('#abstract-section-content > p')).map(p=>p.textContent.trim()).filter(Boolean).join('\n\n'),
        mathPending:!!document.querySelector('#abstract-section-content > p mjx-lazy')}));
      // textContent preserves rendered MathJax symbols which innerText can omit.
      if(!detail || ![detailUrl.href,readingUrl.href].includes(detail.url) || !detail.doi.toLowerCase().endsWith(entry.doi.toLowerCase()) || !detail.abstract) {
        throw new Error('APS abstract identity/content missing');
      }
      if(detail.mathPending) throw new Error('APS abstract math is not rendered');
      entry.abstract=detail.abstract;
    } catch(error) {
      const failure={doi:entry.doi,reason:'abstract_visit_failed',stage,
        exception:error.name,message:String(error.message || '').slice(0,500)};
      if(saveAbstract) {
        try {await saveAbstract(abstractTab,entry.doi,{failed:true});}
        catch(evidenceError) {failure.evidence_error=String(evidenceError.message).slice(0,500);}
      }
      errors.push(failure);
    }
  }
  const final=await inspect();
  if(final.url!==initial.url || JSON.stringify(final.entries.map(e=>e.doi))!==JSON.stringify(initial.entries.map(e=>e.doi))) {
    throw new Error('APS inventory changed during abstract visits');
  }
  return {schema:'papertrack-browser-issue-v1',captured_at:new Date().toISOString(),requested_url:request.url,
    url:initial.url,volume:String(request.volume),issue:String(request.issue),headings:initial.headings,
    card_count:entries.length,raw_card_count:initial.entries.length,entries,errors,error_history:[...(previous?.error_history || previous?.errors || []),...navigationWarnings,...errors],
    remaining_abstracts:entries.filter(e=>!e.abstract && e.abstract_type!=='unavailable').length};
}
