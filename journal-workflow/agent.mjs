// Agent policy depends on abstract model/tool ports; no SDK, filesystem, or browser internals.
import {validateDecision} from './contracts.mjs';
export async function recover({model,tools,evidence,maxCalls=3}) {
  let context={...evidence,inspections:0};
  const decisions=[];
  for(let call=0;call<maxCalls;call++) {
    let decision;
    try {decision=validateDecision(await model.decide(context));}
    catch {decision={action:'require_human',reason:'invalid_model_decision'};}
    decisions.push(decision);
    if(decision.action==='inspect') {
      try {const observation=await tools.inspect();context={...context,...observation,inspections:context.inspections+1};}
      catch {return {action:'require_human',reason:'inspection_failed',decisions};}
      continue;
    }
    return {...decision,decisions};
  }
  return {action:'require_human',reason:'model_budget_exhausted',decisions};
}
