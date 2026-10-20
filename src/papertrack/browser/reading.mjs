/** Retain one verbatim reading panel while it is open, before publisher eviction. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';

export function readingEvidencePath(checkpoint,doi) {
  const normalized=String(doi).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase();
  return path.join(checkpoint+'.readings',createHash('sha256').update(normalized).digest('hex')+'.json');
}

export async function saveReadingEvidence(tab,checkpoint,row) {
  const evidence=await tab.playwright.evaluate(function readingHtmlSnapshotDOM({doi,panel}) {
    const normalize=value=>String(value).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase();
    const cards=Array.from(document.querySelectorAll('.al-article-item-wrap'));
    const candidates=cards.length?cards:Array.from(document.querySelectorAll('.al-article-items'));
    const card=candidates.find(c=>normalize(c.querySelector('.citation-label a')?.getAttribute('href')||'')===normalize(doi));
    const content=document.getElementById(panel);
    const control=card && Array.from(card.querySelectorAll('.showAbstractLink')).find(c=>c.getAttribute('aria-controls')===panel);
    if(!card || !content || !control || content.getAttribute('aria-hidden')==='true') throw Error('reading_evidence_not_visible');
    const result={schema:'papertrack-reading-html-v1',url:location.href,doi:normalize(doi),
      card_html:card.outerHTML,panel_html:content.outerHTML};
    if(JSON.stringify(result).length>128000) throw Error('reading_evidence_too_large');
    return result;
  },{doi:row.doi,panel:row.panel});
  if(evidence?.schema!=='papertrack-reading-html-v1' || typeof evidence.card_html!=='string' || typeof evidence.panel_html!=='string') throw Error('invalid_reading_evidence');
  const file=readingEvidencePath(checkpoint,row.doi);
  await fs.mkdir(path.dirname(file),{recursive:true});
  const temporary=file+'.'+randomUUID()+'.tmp';
  try {await fs.writeFile(temporary,JSON.stringify(evidence)+'\n','utf8');await fs.rename(temporary,file);}
  finally {await fs.rm(temporary,{force:true});}
}

/** Diagnostic only: never a normal reading sidecar or a successful capture. */
export async function saveFailedReadingEvidence(tab,checkpoint,row,failure) {
  const evidence=await tab.playwright.evaluate(function failedReadingSnapshotDOM({doi,panel,control}) {
    const normalize=value=>String(value).replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase();
    const cards=Array.from(document.querySelectorAll('.al-article-item-wrap'));
    const candidates=cards.length?cards:Array.from(document.querySelectorAll('.al-article-items'));
    const card=candidates.find(c=>normalize(c.querySelector('.citation-label a')?.getAttribute('href')||'')===normalize(doi));
    const button=document.getElementById(control), content=document.getElementById(panel);
    const cardHtml=card?.outerHTML || '', panelHtml=content?.outerHTML || '';
    return {schema:'papertrack-failed-reading-html-v1',url:location.href,doi:normalize(doi),
      card_html:cardHtml.slice(0,48000),panel_html:panelHtml.slice(0,48000),
      truncated:{card:cardHtml.length>48000,panel:panelHtml.length>48000},
      control:{id:control,panel,found:!!button,expanded:button?.getAttribute('aria-expanded')??null,
        controls:button?.getAttribute('aria-controls')??null,panel_found:!!content,
        panel_hidden:content?.getAttribute('aria-hidden')??null}};
  },{doi:row.doi,panel:row.panel,control:row.control});
  if(evidence?.schema!=='papertrack-failed-reading-html-v1' || typeof evidence.card_html!=='string' || typeof evidence.panel_html!=='string') throw Error('invalid_failed_reading_evidence');
  const result={...evidence,captured_at:new Date().toISOString(),failure:{reason:failure.reason,stage:failure.stage,
    exception:failure.exception,message:failure.message}};
  const serialized=JSON.stringify(result)+'\n';
  if(serialized.length>128000) throw Error('failed_reading_evidence_too_large');
  const key=path.basename(readingEvidencePath(checkpoint,row.doi),'.json');
  const file=path.join(checkpoint+'.reading-failures',key+'-'+randomUUID()+'.json');
  await fs.mkdir(path.dirname(file),{recursive:true});
  const temporary=file+'.tmp';
  try {await fs.writeFile(temporary,serialized,'utf8');await fs.rename(temporary,file);}
  finally {await fs.rm(temporary,{force:true});}
  return file;
}
