from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey
from ..config.database import Base
from ..config.settings import CONFIG
class Quarantine(Base):
    """隔离区：未知/冲突/校验失败的上报行，保留原始报文，可修正后重放。"""
    __tablename__='quarantined_records'
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    # 原始上报报文，不做任何转换，便于复盘
    raw_payload=Column(JSON,nullable=False)
    claimed_version=Column(String(20),nullable=True)
    observed_at=Column(String(40),nullable=True)
    reason_code=Column(String(40),nullable=False)
    reason_detail=Column(Text,default='')
    state=Column(String(20),nullable=False,default='open')
    replay_of=Column(Integer,nullable=True)
    batch_id=Column(String(80),nullable=True)
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    resolved_record_id=Column(Integer,nullable=True)
