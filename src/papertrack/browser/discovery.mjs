/** Fixed archive-link discovery. Access and navigation remain agent-owned. */
import {saveIssueHtml} from './html.mjs';

export function latestIssueRequest(archiveUrl, code, links) {
  const archive=new URL(archiveUrl);
  if (!/^[a-z0-9]+$/i.test(code)) throw Error('invalid_journal_code');
  const paths={
    'pubs.acs.org':new RegExp(`^/(?:${code}/issue|toc/${code})/(\\d+)/(\\d+)/?$`),
    'pubs.aip.org':new RegExp(`^/(?:aip/)?${code}/issue/(\\d+)/(\\d+)/?$`),
    'journals.aps.org':new RegExp(`^/${code}/issues/(\\d+)/(\\d+)/?$`),
  };
  const pattern=paths[archive.hostname];
  if (archive.protocol!=='https:' || !pattern) throw Error('invalid_archive_url');
  const candidates=[];
  for (const href of links) {
    let url;
    try {url=new URL(href,archive);} catch {continue;}
    const match=url.pathname.match(pattern);
    if (url.origin!==archive.origin || !match) continue;
    candidates.push([Number(match[1]),Number(match[2])]);
  }
  candidates.sort((a,b)=>b[0]-a[0] || b[1]-a[1]);
  if (!candidates.length) return null;
  const [volume,issue]=candidates[0].map(String);
  const prefix=archive.hostname==='journals.aps.org'?`${code}/issues`:
    archive.hostname==='pubs.aip.org'?`aip/${code}/issue`:`${code}/issue`;
  return {url:`${archive.origin}/${prefix}/${volume}/${issue}`,volume,issue};
}

export async function discoverLatestIssue(tab, {archiveUrl,code}, {evidencePath}) {
  const observed=await tab.playwright.evaluate(function archiveLinksDOM() {
    return {url:location.href,title:document.title,
      links:Array.from(document.querySelectorAll('a[href]')).map(a=>a.getAttribute('href')),
      readyState:document.readyState,bodyTextLength:document.body?.innerText?.trim().length || 0,
      challenge:!!document.querySelector('#challenge-running, #challenge-form')};
  });
  const actual=new URL(observed.url), expected=new URL(archiveUrl);
  const archivePaths={
    'pubs.acs.org':[`/loi/${code}`,`/${code}/issue-covers`],
    'pubs.aip.org':[`/aip/${code}/issue`,`/${code}/issue`],
    'journals.aps.org':[`/${code}/issues`],
  };
  const onArchive=actual.origin===expected.origin &&
    archivePaths[expected.hostname]?.includes(actual.pathname.replace(/\/$/,''));
  if (!onArchive) return {status:'needs_access',reason:'not_on_archive',
    observed_url:actual.origin+actual.pathname};
  // Retain the rendered page even when access fails; never snapshot unrelated tabs.
  await saveIssueHtml(tab,evidencePath);
  if (observed.challenge ||
      /just a moment|attention required|access denied|verify you are human/i.test(observed.title)) {
    return {status:'needs_access',reason:'archive_not_readable'};
  }
  const request=latestIssueRequest(archiveUrl,code,observed.links);
  if (!request && (observed.readyState==='loading' ||
      (observed.bodyTextLength===0 && observed.links.length===0))) {
    return {status:'needs_access',reason:'archive_not_loaded',evidencePath};
  }
  if (!request) return {status:'needs_extractor_update',reason:'formal_issue_links_missing',evidencePath};
  return {status:'ready',request,evidencePath};
}
