from sqlalchemy import select,func
from fastapi import HTTPException
from redis import Redis
from ..config.settings import REDIS_URL,CONFIG
from ..models.run import Run
from ..models.quarantine import Quarantine
from ..utils.serialization import row,now
from .sources import source_or_404,list_records
from .domain_validation import validate_parameters
from . import unit_versions as uv
from .audit import log
queue=Redis.from_url(REDIS_URL)
def get_run(db,id):
    r=db.get(Run,id)
    if not r:raise HTTPException(404,'未找到分析任务')
    return r
def _rule_profile(source):
    """创建时生效的单位版本规则，随快照冻结；之后规则变化不影响本研究。"""
    return {
        'canonical_units':dict(uv.CANONICAL_UNITS),
        'unit_versions':{k:{'label':v['label'],'units':dict(v['units'])} for k,v in uv.UNIT_VERSIONS.items()},
        'conversions':dict(CONFIG.get('conversions',{})),
        'cutover_at':getattr(source,'v2_effective_at',None) or uv.CUTOVER_AT,
        'source_v2_effective_at':getattr(source,'v2_effective_at',None),
        'late_grace_hours':uv.LATE_GRACE_HOURS,
    }
def create_run(db,user,data):
    s=source_or_404(db,data.source_id)
    # 新研究只读完整版本：存在待处理隔离行时拒绝，避免把口径不明的数据混入计算
    open_q=db.scalar(select(func.count()).select_from(Quarantine).where(
        Quarantine.source_id==s.id,Quarantine.state=='open'))
    if open_q:
        raise HTTPException(409,f'来源存在 {open_q} 条待处理隔离记录，请先修正重放或关闭后再冻结研究')
    records=list_records(db,s.id)
    if len(records)<3:raise HTTPException(422,'至少录入3条样本后才能执行')
    try: params=validate_parameters(data.parameters)
    except ValueError as e:raise HTTPException(422,str(e))
    versions=sorted({r.get('unit_version',uv.DEFAULT_VERSION) for r in records})
    snapshot={
        'source':row(s),
        'records':records,
        'unit_rule':_rule_profile(s),
        'frozen_at':now(),
        'record_unit_versions':versions,
    }
    r=Run(source_id=s.id,name=data.name,parameters=params,snapshot=snapshot,created_at=now(),
          state='queued',attempts=0)
    db.add(r);db.flush();log(db,user,'run.queued',r.id,
        data.name+f'；冻结 {len(records)} 条输入，来源版本 {",".join(versions)}，规则口径已固化');db.commit()
    try:queue.rpush('analysis:queue',r.id)
    except Exception:
        r.state='failed';r.error='队列暂不可用，请重新创建任务';db.commit();raise HTTPException(503,r.error)
    return row(r)
def list_runs(db):
    return [{k:v for k,v in row(r).items() if k not in ('snapshot','result')} for r in db.scalars(select(Run).order_by(Run.id.desc()))]
