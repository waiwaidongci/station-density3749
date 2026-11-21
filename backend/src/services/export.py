import csv,io,json
from fastapi import HTTPException
from fastapi.responses import Response
from ..models.note import Note
from .runs import get_run
def _render_csv(result):
    buf=io.StringIO()
    unit_meta=result.get('unit_meta',{})
    canonical=unit_meta.get('canonical_units',{})
    # 量纲抬头：说明本文件统一量纲与来源版本口径，保证导出可追查
    buf.write('# 统一规范量纲: '+json.dumps(canonical,ensure_ascii=False)+'\n')
    buf.write('# 输入来源版本统计: '+json.dumps(unit_meta.get('row_unit_versions',{}),ensure_ascii=False)+'\n')
    fields=list(result['columns'])+['unit_version','conversion_trace']
    w=csv.DictWriter(buf,fieldnames=fields);w.writeheader()
    for r in result['rows']:
        row={k:r.get(k,'') for k in result['columns']}
        row['unit_version']=r.get('unit_version','')
        trace=r.get('conversion_trace',{})
        row['conversion_trace']=json.dumps(trace,ensure_ascii=False) if trace else 'identity'
        w.writerow(row)
    return Response('﻿'+buf.getvalue(),media_type='text/csv; charset=utf-8')
def csv_response(result,filename):
    resp=_render_csv(result)
    resp.headers['Content-Disposition']='attachment; filename="'+filename+'"'
    return resp
def run_csv(db,id):
    r=get_run(db,id)
    if r.state!='completed':raise HTTPException(409,'结果尚未完成')
    return csv_response(r.result,'run-'+str(id)+'.csv')
def note_csv(db,id):
    n=db.get(Note,id)
    if not n:raise HTTPException(404,'未找到说明')
    if n.state!='approved':raise HTTPException(403,'仅批准后的业务说明可导出')
    return csv_response(n.frozen_result,'approved-'+str(id)+'.csv')
