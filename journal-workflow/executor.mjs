// Conventional execution: callbacks are ports, never model imports or prompts.
import path from 'node:path';
import {validateCapture, validateJob} from './contracts.mjs';
import {atomicJson, readJson} from './storage.mjs';
export async function executeIssue({job, dir, driver, maxBatches=100}) {
  job = validateJob(job);
  const checkpointPath = path.join(dir,'capture.json');
  let previous = await readJson(checkpointPath);
  if (previous) {
    const saved = validateCapture(previous,job);
    if (!saved.missing.length) return {status:'captured',...saved,capture:checkpointPath};
  }
  for(let batch=0;batch<maxBatches;batch++) {
    const data = await driver.capture({job,previous});
    const progress = validateCapture(data,job,previous);
    await atomicJson(checkpointPath,data);
    await atomicJson(path.join(dir,'progress.json'),{...progress,updated_at:new Date().toISOString()});
    if (!progress.missing.length) return {status:'captured',...progress,capture:checkpointPath};
    const before = previous?.entries.filter(r=>r.abstract).length || 0;
    previous = data;
    if(progress.captured <= before) throw Error('no_progress');
  }
  throw Error('batch_budget_exhausted');
}
