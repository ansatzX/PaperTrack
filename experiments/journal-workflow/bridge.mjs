// File RPC between a normal Node worker and a tool-hosted browser driver.
// No Chrome profile access or alternate browser-control channel.
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {atomicJson, readJson, pause} from './storage.mjs';
export class BrowserBridge {
  constructor(dir,{timeoutMs=90000}={}) {this.dir=dir;this.timeoutMs=timeoutMs;}
  async call(action,payload={}) {
    const id=randomUUID(), expires=Date.now()+this.timeoutMs;
    await atomicJson(path.join(this.dir,'command.json'),{id,action,payload,expires});
    try {
      while(Date.now()<expires) {
        const response=await readJson(path.join(this.dir,'response.json'));
        if(response?.id===id) {
          if(!response.ok) throw Error(response.error || 'browser_action_failed');
          return response.value;
        }
        await pause(200);
      }
      throw Error('browser_bridge_timeout');
    } finally {
      await atomicJson(path.join(this.dir,'command.json'),{id,action:'idle',expires:0});
    }
  }
  capture(payload) {return this.call('capture',payload);}
  inspect() {return this.call('inspect');}
}
