import {domain} from '../constants/domain.js';import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {sourcesApi,quarantineApi} from '../api/research.js';import {table} from '../components/common/table.js';import {sourceOptions} from '../components/common/business.js';import {esc,formObject,localTime} from '../utils/format.js';import {action,toast,bind} from '../hooks/actions.js';import {fieldUnitLabel,previewCanonical,REASON_TEXT,QUARANTINE_STATE_TEXT} from '../utils/units.js';

const VERSIONS=Object.keys(domain.unit_versions);

function versionFieldHint(version){
  return domain.fields.map(([key,label])=>label+' 录入单位：'+fieldUnitLabel(key,version)).join(' · ');
}

function quarantinePanel(){
  const rows=research.quarantine.filter(q=>q.source_id===research.sourceId);
  if(!rows.length)return '';
  const open=rows.filter(q=>q.state==='open').length;
  return '<section class="quarantine-panel"><h2>隔离区 <small>'+open+' 条待处理 · 共 '+rows.length+' 条；原始报文保留，修正后可重放，不影响已冻结研究</small></h2>'+
    table(rows.map(q=>({reference:q.reference,state:QUARANTINE_STATE_TEXT[q.state]||q.state,reason:REASON_TEXT[q.reason_code]||q.reason_code,claimed_version:q.claimed_version||'（缺省 v1）',observed_at:q.observed_at?localTime(q.observed_at):'—',detail:q.reason_detail,id:q.id})),
      [{key:'reference',label:'样本编号'},{key:'state',label:'状态'},{key:'reason',label:'隔离原因'},{key:'claimed_version',label:'上报版本'},{key:'observed_at',label:'观测时间'},{key:'detail',label:'说明'}],{limit:50})+
    (session.writable?'<p class="fine">重放将以隔离行原始报文重新走接纳管道；可在弹窗中修正版本/观测时间/数值。同编号已存在且内容一致时直接幂等接纳，不产生重复记录。</p>':'')+
    rows.filter(q=>q.state==='open').map(q=>'<div class="quarantine-actions" data-qid="'+q.id+'"><code>'+esc(q.reference)+'</code>'+
      (session.writable?'<button class="secondary" data-replay="'+q.id+'">修正并重放 →</button><button data-discard="'+q.id+'">人工关闭</button>':'')+'</div>').join('')+'</section>';
}

export async function sourcePage(){
  research.records=research.sourceId?await sourcesApi.records(research.sourceId):[];
  const selected=research.sources.find(s=>s.id==research.sourceId);
  const recordColumns=[{key:'reference',label:'样本编号'},...domain.fields.map(f=>({key:f[0],label:f[1]})),{key:'unit_version',label:'来源版本'},{key:'observed_at',label:'观测时间'}];
  const recordRows=research.records.map(r=>({reference:r.reference,...r.values,unit_version:r.unit_version,observed_at:r.observed_at?localTime(r.observed_at):'—'}));
  return '<section class="page-heading"><p class="eyebrow">02 / SOURCE & EVIDENCE</p><h1>先核对'+esc(domain.record)+'，再解释结果。</h1><p>样本只追加，任务保留创建时的输入快照与单位版本规则；页面与导出统一为规范量纲（℃、%、μg/m³），每行可追查来源版本。</p></section>'+
    (research.quarantineCounts.open?'<div class="quarantine-banner">⚠ 当前有 '+research.quarantineCounts.open+' 条待处理隔离记录，新研究在隔离清零前只读不可冻结。<a href="#quarantine">前往隔离区 ↗</a></div>':'')+
    '<div class="split"><section><label>来源'+esc(domain.source)+'<select id="source-select">'+sourceOptions(research.sources,research.sourceId)+'</select></label>'+
    '<p class="file-note">'+esc(selected?.filename||'尚未创建来源')+' · '+research.records.length+' 条记录'+(selected?.v2_effective_at?(' · v2 生效 '+localTime(selected.v2_effective_at)):(' · 全局 v2 生效 '+localTime(domain.unit_cutover_at)))+(selected?.open_quarantine_count?(' · 隔离待处理 '+selected.open_quarantine_count):'')+'</p>'+
    table(recordRows,recordColumns,{limit:15,offset:research.page*15})+
    '<div class="pagination"><button id="prev" '+(research.page===0?'disabled':'')+'>上一页</button><span>'+(research.page+1)+' / '+Math.max(1,Math.ceil(research.records.length/15))+'</span><button id="next" '+((research.page+1)*15>=research.records.length?'disabled':'')+'>下一页</button></div>'+
    (session.writable&&selected?'<details><summary>修改来源说明 / 单位生效时间</summary><form id="edit-source"><label>来源名称<input name="name" value="'+esc(selected.name)+'" required></label><label>文件名<input name="filename" value="'+esc(selected.filename)+'" required></label><label>说明<textarea name="description">'+esc(selected.description)+'</textarea></label><label>v2 单位生效时间（UTC，留空用全局 '+esc(domain.unit_cutover_at)+'）<input name="v2_effective_at" type="datetime-local" value="'+esc((selected.v2_effective_at||'').slice(0,16))+'"></label><button type="submit">更新来源说明</button></form></details>':'')+
    quarantinePanel()+
    '</section><aside class="entry-sheet">'+(session.writable?'<h2>追加'+esc(domain.record)+'</h2><form id="record-form"><label>样本编号<input name="reference" required placeholder="'+domain.code+'-NEW-001"></label><label>上报单位版本<select name="unit_version" id="unit-version-select">'+VERSIONS.map(v=>'<option value="'+v+'" '+((selected&&selected.v2_effective_at)?(v==='v2'?'selected':''):(v===domain.default_unit_version?'selected':''))+'>'+esc(domain.unit_versions[v].label)+'</option>').join('')+'</select></label><label>观测时间（UTC）<input name="observed_at" type="datetime-local" required value="2026-11-16T08:00"></label>'+
    domain.fields.map(([key,label,type])=>'<label>'+esc(label)+'<small class="unit-hint" data-unit-for="'+key+'"></small><input name="'+key+'" type="'+(type==='number'?'number':'text')+'" '+(type==='number'?'step="any"':'')+' required value="'+esc(domain.sample[key])+'"></label>').join('')+
    '<div class="canonical-preview" id="canonical-preview" hidden><b>规范量纲预览</b><p></p><small>权威转换以后端接纳结果为准</small></div>'+
    '<p class="fine" id="version-hint">'+esc(versionFieldHint(domain.default_unit_version))+'</p><button type="submit">保存样本 →</button><p class="fine">未知版本、与生效时间冲突（含迟到旧版本）或非法数值将进入隔离区，可修正重放。</p></form>'+
    '<details><summary>新建'+esc(domain.source)+'</summary><form id="new-source"><label>来源名称<input name="name" required placeholder="十一月补充研究"></label><label>文件名<input name="filename" required value="'+domain.slug+'-new.csv"></label><label>业务说明<textarea name="description" required>独立补充采集，供复核研究。</textarea></label><button type="submit">创建来源</button></form></details>':'<h2>阅读权限</h2><p>当前可查看全部业务样本、来源版本与隔离记录。来源录入由负责人或分析师完成。</p>')+'</aside></div>';
}

function refreshHints(form){
  const version=form.querySelector('#unit-version-select').value;
  form.querySelectorAll('.unit-hint').forEach(el=>{el.textContent='录入单位：'+fieldUnitLabel(el.dataset.unitFor,version);});
  form.querySelector('#version-hint').textContent=versionFieldHint(version);
  const data=formObject(form);
  const values={};for(const [key]of domain.fields)values[key]=data[key];
  const preview=previewCanonical(values,version);
  const box=form.querySelector('#canonical-preview');
  if(preview){box.hidden=false;box.querySelector('p').textContent=domain.fields.map(([key,label])=>label+'='+(typeof preview[key]==='number'?preview[key].toFixed(3):preview[key])+' '+domain.canonical_units[key]).join(' · ');}
  else box.hidden=true;
}

export function bindSource(refresh){
  bind('#source-select','change',async e=>{research.sourceId=Number(e.target.value);research.page=0;await refresh()});
  bind('#prev','click',async()=>{research.page--;await refresh()});
  bind('#next','click',async()=>{research.page++;await refresh()});
  bind('#unit-version-select','change',e=>refreshHints(e.target.form));
  document.querySelectorAll('#record-form input').forEach(i=>i.addEventListener('input',e=>refreshHints(e.target.form)));
  const form=document.querySelector('#record-form');if(form)refreshHints(form);
  action('#record-form',async formEl=>{
    const data=formObject(formEl),values={};
    for(const [key,,type]of domain.fields)values[key]=type==='number'?Number(data[key]):data[key];
    const payload={reference:data.reference,values,unit_version:data.unit_version,observed_at:new Date(data.observed_at).toISOString()};
    const res=await sourcesApi.add(research.sourceId,[payload]);
    await refresh();
    toast(res.quarantined?('已接纳 '+res.accepted+' 条，隔离 '+res.quarantined+' 条，请在隔离区处理'):'业务样本已保存并统一为规范量纲');
  });
  action('#new-source',async formEl=>{const s=await sourcesApi.create(formObject(formEl));research.sourceId=s.id;research.page=0;await refresh();toast('新来源已创建，请追加样本')});
  action('#edit-source',async formEl=>{
    const data=formObject(formEl);
    data.v2_effective_at=data.v2_effective_at?new Date(data.v2_effective_at).toISOString():null;
    await sourcesApi.update(research.sourceId,data);await refresh();toast('来源说明已更新；已冻结任务快照与规则保留');
  });
  for(const btn of document.querySelectorAll('[data-discard]'))btn.onclick=async()=>{
    try{await quarantineApi.discard(btn.dataset.discard);await refresh();toast('隔离行已关闭，原始报文保留')}catch(e){toast(e.message,true)}
  };
  for(const btn of document.querySelectorAll('[data-replay]'))btn.onclick=async()=>{
    const qid=btn.dataset.replay;
    const q=research.quarantine.find(x=>x.id==qid);
    const raw=q.raw_payload||{};
    const fixVersion=prompt('修正上报单位版本（v1 / v2）',q.claimed_version&&domain.unit_versions[q.claimed_version]?q.claimed_version:'v2');
    if(fixVersion===null)return;
    const fixTemp=prompt('修正 '+domain.fields[0][1]+' 录入值（按所选版本单位）',raw.temperature??'');
    if(fixTemp===null)return;
    const fixParticles=prompt('修正 '+domain.fields[2][1]+' 录入值（按所选版本单位）',raw.particles??'');
    if(fixParticles===null)return;
    try{
      const out=await quarantineApi.replay(qid,{unit_version:fixVersion,observed_at:q.observed_at||new Date().toISOString(),
        values:{...raw,temperature:Number(fixTemp),particles:Number(fixParticles)}});
      await refresh();
      toast(out.outcome==='accepted'?'重放成功，已按规范量纲接纳':'重放仍被隔离，请继续修正');
    }catch(e){toast(e.message,true)}
  };
}
