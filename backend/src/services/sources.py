import secrets
from fastapi import HTTPException
from sqlalchemy import select,func
from ..models.source import Source
from ..models.record import Record
from ..models.quarantine import Quarantine
from ..utils.serialization import row
from .audit import log
from .ingestion import ingest_batch
def list_sources(db):
    rows=[]
    for s in db.scalars(select(Source).order_by(Source.id)):
        d=row(s)
        d['record_count']=db.scalar(select(func.count()).select_from(Record).where(Record.source_id==s.id))
        d['open_quarantine_count']=db.scalar(select(func.count()).select_from(Quarantine).where(
            Quarantine.source_id==s.id,Quarantine.state=='open'))
        rows.append(d)
    return rows
def source_or_404(db,id):
    s=db.get(Source,id)
    if not s:raise HTTPException(404,'未找到来源批次')
    return s
def save_source(db,user,data,id=None):
    s=source_or_404(db,id) if id else Source(key='user-'+secrets.token_hex(10))
    payload=data.model_dump()
    for k,v in payload.items():setattr(s,k,v)
    db.add(s);db.flush();log(db,user,'source.updated' if id else 'source.created',s.id,s.name);db.commit();return row(s)
def list_records(db,id):
    source_or_404(db,id)
    return [row(x) for x in db.scalars(select(Record).where(Record.source_id==id).order_by(Record.id))]
def add_records(db,user,id,data):
    s=source_or_404(db,id)
    items=[r.model_dump() for r in data.records]
    # 接纳管道内部完成版本核对、量纲转换、校验、隔离与幂等
    return ingest_batch(db,user,s,items)
