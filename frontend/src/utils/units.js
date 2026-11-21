import {domain} from '../constants/domain.js';

// 前端侧仅做录入提示与预览；权威转换与校验在后端完成，页面与导出始终使用规范量纲
export function fToC(f){return (Number(f)-32)*5/9;}
export function cToF(c){return Number(c)*9/5+32;}

export function versionUnits(version){
  return domain.unit_versions[version]?.units || null;
}

// 依据所选版本给出每个字段的录入单位标签
export function fieldUnitLabel(field,version){
  const units=versionUnits(version);
  return units?units[field]:domain.canonical_units[field];
}

// 录入实时预览：把当前报文值换算为规范量纲展示
export function previewCanonical(values,version){
  const units=versionUnits(version);
  if(!units)return null;
  const out={...values};
  if(units.temperature==='℉'&&values.temperature!==''){
    const f=Number(values.temperature);
    if(Number.isFinite(f))out.temperature=fToC(f);
  }
  if(units.particles==='mg/m³'&&values.particles!==''){
    const mg=Number(values.particles);
    if(Number.isFinite(mg))out.particles=mg*1000;
  }
  return out;
}

export const REASON_TEXT={
  unknown_unit_version:'未知单位版本',
  unit_version_conflict:'版本与生效时间冲突',
  late_old_unit_version:'迟到的旧版本（换代后仍上报 v1）',
  invalid_record_value:'字段数值非法',
  duplicate_payload_conflict:'同编号内容冲突，未改写既有记录',
};

export const QUARANTINE_STATE_TEXT={
  open:'待处理',
  replayed:'已重放',
  superseded:'重放后仍隔离',
  discarded:'人工关闭',
};
