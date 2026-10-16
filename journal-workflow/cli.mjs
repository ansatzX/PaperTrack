import {parseArgs} from 'node:util';
import {readJson} from './storage.mjs';
import {runJob} from './runner.mjs';
const {values}=parseArgs({options:{job:{type:'string'},dir:{type:'string'},output:{type:'string'},
  python:{type:'string',default:'python'},agent:{type:'string',default:'off'},model:{type:'string'}}});
try {
  if(!values.job||!values.dir||!values.output) throw Error('job_dir_output_required');
  if(!['off','openai','deepseek'].includes(values.agent)) throw Error('invalid_agent');
  let advisor=null;
  if(values.agent!=='off') {
    const {createModelProvider}=await import('./model-provider.mjs');
    const {recover}=await import('./agent.mjs');
    const model=await createModelProvider({provider:values.agent,model:values.model});
    advisor=args=>recover({...args,model});
  }
  const result=await runJob({job:await readJson(values.job),dir:values.dir,output:values.output,python:values.python,advisor});
  console.log(JSON.stringify(result));
  if(result.status!=='complete') process.exitCode=2;
} catch(error) {
  console.error(JSON.stringify({status:'failed',error:error.code==='ELOCKED'?'job_already_running':'invalid_job_or_runtime_failure'}));
  process.exitCode=2;
}
