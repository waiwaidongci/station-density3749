import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {quarantineApi} from '../api/research.js';import {table} from '../components/common/table.js';import {esc,localTime} from '../utils/format.js';import {toast} from '../hooks/actions.js';import {REASON_TEXT,QUARANTINE_STATE_TEXT} from '../utils/units.js';

const FILTERS=[['open','待处理'],['replayed','已重放'],['superseded','重放后仍隔离'],['discarded','人工关闭']];

export async function quarantinePage(){
  const sourceName=Object.fromEntries(research.sources.map(s=>[s.id,s.name]));
  const rows=research.quarantine.map(q=>({
    id:q.id,source:sourceName[q.source_id]||('#'+q.source_id),reference:q.reference,
    state:QUARANTINE_STATE_TEXT[q.state]||q.state,reason:REASON_TEXT[q.reason_code]||q.reason_code,
    claimed_version:q.claimed_version||'（缺省 v1）',observed_at:q.observed_at?localTime(q.observed_at):'—',
    payload:JSON.stringify(q.raw_payload),detail:q.reason_detail,updated_at:localTime(q.updated_at),
  }));
  const open=research.quarantineCounts.open||0;
  return '<section class="page-heading"><p class="eyebrow">07 / QUARANTINE & REPLAY</p><h1>口径不明的上报，先隔离再修正。</h1><p>未知单位版本、与生效时间冲突（含迟到旧版本）或非法数值不进入研究输入；原始报文完整保留，修正后重放走同一接纳管道，自然键保证不重复。已冻结研究的输入永不被隔离区或迟到行改写。</p></section>'+
    '<div class="quarantine-summary"><div><strong>'+open+'</strong><span>待处理</span></div><div><strong>'+(research.quarantineCounts.total||0)+'</strong><span>累计隔离</span></div></div>'+
    (open?'<div class="quarantine-banner">新研究在待处理隔离清零前只读完整版本，不可冻结。</div>':'<div class="empty">没有待处理隔离记录，来源数据版本完整。</div>')+
    '<h2>隔离台账 <small>点击列可核对原始报文；仅待处理（open）行可重放或关闭</small></h2>'+
    table(rows,[{key:'id',label:'#'},{key:'source',label:'来源'},{key:'reference',label:'样本编号'},{key:'state',label:'状态'},{key:'reason',label:'隔离原因'},{key:'claimed_version',label:'上报版本'},{key:'observed_at',label:'观测时间'},{key:'payload',label:'原始报文'},{key:'detail',label:'说明'},{key:'updated_at',label:'更新时间'}],{limit:100})+
    (session.writable?'<div id="quarantine-actions">'+research.quarantine.filter(q=>q.state==='open').map(q=>'<div class="quarantine-actions" data-qid="'+q.id+'"><code>'+esc(q.reference)+'</code><button class="secondary" data-replay="'+q.id+'">修正并重放 →</button><button data-discard="'+q.id+'">人工关闭</button></div>').join('')+'</div>':'<p class="fine">阅读权限：可查看全部隔离记录与原始报文，重放与关闭由分析师或负责人执行。</p>');
}

export function bindQuarantine(refresh){
  for(const btn of document.querySelectorAll('[data-discard]'))btn.onclick=async()=>{
    try{await quarantineApi.discard(btn.dataset.discard);await refresh();toast('隔离行已关闭，原始报文与审计痕迹保留')}catch(e){toast(e.message,true)}
  };
  for(const btn of document.querySelectorAll('[data-replay]'))btn.onclick=async()=>{
    const q=research.quarantine.find(x=>x.id==btn.dataset.replay);
    const raw=q.raw_payload||{};
    const fixVersion=prompt('修正上报单位版本（v1 / v2）',q.claimed_version&&['v1','v2'].includes(q.claimed_version)?q.claimed_version:'v2');
    if(fixVersion===null)return;
    const fixObserved=prompt('修正观测时间（本地日期时间，留空沿用原值）',q.observed_at?q.observed_at.slice(0,16):'2026-11-16T08:00');
    if(fixObserved===null)return;
    const fixTemp=prompt('修正温度录入值（按所选版本单位）',raw.temperature??'');
    if(fixTemp===null)return;
    const fixParticles=prompt('修正颗粒浓度录入值（按所选版本单位）',raw.particles??'');
    if(fixParticles===null)return;
    try{
      const out=await quarantineApi.replay(q.id,{
        unit_version:fixVersion,
        observed_at:fixObserved?new Date(fixObserved).toISOString():q.observed_at,
        values:{...raw,temperature:Number(fixTemp),particles:Number(fixParticles)},
      });
      await refresh();
      toast(out.outcome==='accepted'?'重放成功：已转换为规范量纲并接纳，隔离行关闭':'重放仍被隔离，已生成新隔离行并保留重放链');
    }catch(e){toast(e.message,true)}
  };
}
