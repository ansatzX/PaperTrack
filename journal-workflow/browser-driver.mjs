// Import ONLY in the supported browser tool runtime, with supplied browser tabs.
import path from 'node:path';
import {captureIssue} from '../browser/issue.mjs';
import {atomicJson,readJson,pause} from './storage.mjs';
import {validateJob,validateCapture} from './contracts.mjs';
export function createBrowserDriver({tab,abstractTab}) {
  let currentUrl = null;
  return {
    async capture({job,previous}) {
      validateJob(job);
      const capture = await captureIssue(tab,job,{abstractTab,previous,
        navigate:currentUrl!==job.url,maxInteractions:1});
      currentUrl=job.url;
      validateCapture(capture,job,previous);
      return capture;
    },
    async inspect() {
      // Read only the task tab. No cookies, profile, other tabs, or full HTML.
      return {dom:(await tab.playwright.domSnapshot()).slice(0,18000)};
    },
  };
}
export async function serveBrowser(dir, driver, {durationMs=20000}={}) {
  const until=Date.now()+Math.min(durationMs,25000);
  let handled=0;
  while(Date.now()<until) {
    const command=await readJson(path.join(dir,'command.json'));
    const response=await readJson(path.join(dir,'response.json'));
    if(!command || command.action==='idle' || command.expires<Date.now() || command.id===response?.id) {
      await pause(200);continue;
    }
    let result;
    try {
      if(!['capture','inspect'].includes(command.action)) throw Error('unsupported_browser_action');
      result={id:command.id,ok:true,value:await driver[command.action](command.payload)};
    } catch(error) {
      // Do not persist raw exceptions which could contain URLs or credentials.
      const known=['no_progress','invalid_capture','inventory_drift','existing_content_changed'];
      result={id:command.id,ok:false,error:known.includes(error.message)?error.message:'browser_action_failed'};
    }
    if(Date.now()<command.expires) await atomicJson(path.join(dir,'response.json'),result);
    handled++;
  }
  return {handled};
}
