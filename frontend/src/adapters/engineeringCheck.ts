import type { DesignAnalysis } from '../types/index.ts';
export type EngineeringCheck = {kind:'complete';analysis:DesignAnalysis} | {kind:'pending';workflowId:string};
export function validAnalysis(value:unknown):DesignAnalysis {
  const result=value as DesignAnalysis | null;
  if (!result || !Array.isArray(result.dfm_issues) || !Array.isArray(result.rule_violations) || typeof result.design_summary!=='string') throw new Error('工程检查结果格式无效');
  if (result.design_score != null && (!Number.isFinite(result.design_score) || result.design_score<0 || result.design_score>100)) throw new Error('工程检查分数无效');
  return result;
}
export function engineeringCheckResponse(status:number,value:unknown,workflowHeader?:string|null):EngineeringCheck {
  if (status===202 || status===504 && workflowHeader) {
    const bodyId=(value as {workflow_run_id?:unknown}|null)?.workflow_run_id;
    const id=bodyId || workflowHeader;
    if (typeof id!=='string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(id) || bodyId && workflowHeader && bodyId!==workflowHeader) throw new Error('工程检查没有返回一致且可恢复的任务身份');
    return {kind:'pending',workflowId:id};
  }
  if (status<200 || status>=300) throw new Error('工程检查失败');
  return {kind:'complete',analysis:validAnalysis(value)};
}
