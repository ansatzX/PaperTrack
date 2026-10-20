// Explicit opt-in smoke: costs one or more real API requests, no private browser data.
// node test/live-model.mjs deepseek /tmp/model-evidence.json
import {createModelProvider} from '../model-provider.mjs';
import {recover} from '../agent.mjs';
import {atomicJson} from '../storage.mjs';
const provider=await createModelProvider({provider:process.argv[2]||'deepseek'});
const result=await recover({model:provider,evidence:{issue:'jcim-66-12',failure:'browser_action_failed',remaining:2},
  tools:{inspect:async()=>({dom:'The task browser tab is closed. No page content is available. Reconnection by the browser host is required.'})}});
const evidence={kind:'live_model_with_synthetic_failure',provider:provider.provider,model:provider.model,available:provider.available,result};
if(process.argv[3]) await atomicJson(process.argv[3],evidence);
console.log(JSON.stringify(evidence));
if(!provider.available||result.reason==='model_request_failed_or_invalid') process.exitCode=2;
