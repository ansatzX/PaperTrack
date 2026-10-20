// Pure contracts shared by execution and agent layers. No SDK/browser imports.
export function validateJob(job) {
  if (job?.schema !== 'papertrack-journal-job-v1' ||
      !['jcim','jctc','jcp','prl'].includes(job.journal) ||
      !/^\d+$/.test(job.volume) || !/^\d+$/.test(job.issue) ||
      !Number.isInteger(job.year) || job.year < 1900 || job.year > 2200) throw Error('invalid_job');
  const [host, code, path] = {
    jcim:['pubs.acs.org','jcisd8','issue'], jctc:['pubs.acs.org','jctcce','issue'],
    jcp:['pubs.aip.org','jcp','issue'], prl:['journals.aps.org','prl','issues'],
  }[job.journal];
  const url = new URL(job.url);
  const paths = [`/${code}/${path}/${job.volume}/${job.issue}`];
  if (job.journal === 'jcp') paths.push(`/aip${paths[0]}`);
  if (url.protocol !== 'https:' || url.hostname !== host || url.port || url.username || url.password ||
      url.search || url.hash || !paths.includes(url.pathname)) throw Error('invalid_issue_url');
  return {schema:job.schema,journal:job.journal,url:url.href,volume:job.volume,issue:job.issue,year:job.year};
}
export const keyOf = job => `${job.journal}-${job.volume}-${job.issue}`;
export const doiOf = row => String(row.doi || '').replace(/^https?:\/\/(?:dx\.)?doi.org\//i,'').toLowerCase();
export const wantsText = row => ['abstract','extract'].includes(row.abstract_type);
export function validateCapture(data, job, prior = null) {
  if (data?.schema !== 'papertrack-browser-issue-v1' || data.url !== job.url ||
      data.volume !== job.volume || data.issue !== job.issue ||
      !data.headings?.includes(`Volume ${job.volume}, Issue ${job.issue}`) ||
      !data.entries?.length || data.entries.length !== data.card_count) throw Error('invalid_capture');
  const ids = data.entries.map(doiOf);
  if (new Set(ids).size !== ids.length || data.entries.some(r=>!r.title || !/^10\.\d{4,9}\/\S+$/.test(doiOf(r)))) throw Error('invalid_inventory');
  if (prior) {
    if (JSON.stringify(ids) !== JSON.stringify(prior.entries.map(doiOf))) throw Error('inventory_drift');
    for(let i=0;i<ids.length;i++) {
      if(prior.entries[i].abstract && prior.entries[i].abstract !== data.entries[i].abstract) throw Error('existing_content_changed');
    }
  }
  return {total:ids.length, captured:data.entries.filter(r=>r.abstract).length,
    missing:data.entries.filter(r=>wantsText(r)&&!r.abstract).map(doiOf)};
}
export const recoveryActions = ['inspect','retry','require_human'];
export function validateDecision(value) {
  if (!value || !recoveryActions.includes(value.action) || typeof value.reason !== 'string' ||
      value.reason.length > 1200 || Object.keys(value).some(k=>!['action','reason'].includes(k))) throw Error('invalid_decision');
  return value;
}
