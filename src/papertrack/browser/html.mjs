/** Fixed, bounded rendered-HTML export for supported browser-client tabs. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';

async function atomicWrite(file,value) {
  await fs.mkdir(path.dirname(file),{recursive:true});
  const temporary=`${file}.${randomUUID()}.tmp`;
  try {await fs.writeFile(temporary,value,'utf8');await fs.rename(temporary,file);}
  finally {await fs.rm(temporary,{force:true});}
}

// Browser APIs may silently cap a returned string (observed at ~200 KB).
// Split one immutable snapshot, not successive reads of a changing live DOM.
async function readHtmlSnapshot(tab,apsAbstract=false) {
  const snapshot=await tab.playwright.evaluate(function issueHtmlSnapshotDOM({apsAbstract=false}={}) {
    let reading=null;
    if(apsAbstract) {
      const paragraphs=Array.from(document.querySelectorAll('#abstract-section-content > p'));
      reading={url:location.href,
        doi:Array.from(document.querySelectorAll('main p')).map(e=>e.textContent.trim()).find(t=>t.startsWith('DOI:')) || '',
        abstract:paragraphs.map(p=>p.textContent.trim()).filter(Boolean).join('\n\n'),
        mathPending:paragraphs.some(p=>p.querySelector('mjx-lazy')),
        unavailableArticle:!!document.querySelector('#article-page-wrapper[data-article-type="erratum"] #fulltext-content, #article-page-wrapper[data-article-type="comment"] #fulltext-content, #article-page-wrapper[data-article-type="reply"] #fulltext-content, #article-page-wrapper[data-article-type="retraction"] #fulltext-content')?.textContent.trim()
          && !document.querySelector('#abstract-section-content, #section-abstract, a[href="#abstract-section-content"]')};
    }
    const html=document.documentElement.outerHTML;
    if (html.length>8*1024*1024) return {length:html.length,chunks:[]};
    const chunks=[];
    for(let offset=0;offset<html.length;offset+=32000) chunks.push(html.slice(offset,offset+32000));
    return {length:html.length,chunks,reading};
  },{apsAbstract});
  const {length,chunks}=snapshot || {};
  if (!Number.isInteger(length) || length<1 || length>8*1024*1024) throw Error('html_snapshot_size_out_of_bounds');
  if (!Array.isArray(chunks) || chunks.length!==Math.ceil(length/32000) ||
      chunks.some((chunk,index)=>typeof chunk!=='string' || chunk.length!==Math.min(32000,length-index*32000))) {
    throw Error('html_snapshot_chunk_truncated');
  }
  return {html:chunks.join(''),reading:snapshot.reading};
}

export async function readIssueHtml(tab) {
  return (await readHtmlSnapshot(tab)).html;
}

export async function saveIssueHtml(tab,file,{apsAbstract=false}={}) {
  if (!path.isAbsolute(file)) throw Error('absolute_html_path_required');
  const {html,reading}=await readHtmlSnapshot(tab,apsAbstract);
  await atomicWrite(file,html);
  return {html_path:file,characters:html.length,bytes:Buffer.byteLength(html,'utf8'),reading};
}

