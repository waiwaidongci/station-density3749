from sqlalchemy import Column,Integer,String,JSON,ForeignKey,UniqueConstraint
from ..config.database import Base
from ..config.settings import CONFIG
class Record(Base):
    __tablename__=CONFIG['record_table']
    __table_args__=(UniqueConstraint('source_id','reference',name='uq_record_reference'),)
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    # values 始终以规范量纲保存（℃、%、μg/m³）
    values=Column(JSON,nullable=False)
    # 上报时的字段单位版本（v1/v2…），逐行可追查
    unit_version=Column(String(20),nullable=False,default=CONFIG['default_unit_version'])
    # 观测时间（UTC），用于按生效时间接纳单位版本
    observed_at=Column(String(40),nullable=True)
    # 每个字段的转换规则痕迹，如 {'temperature':'F_to_C:(F-32)*5/9'}
    conversion_trace=Column(JSON,nullable=False,default=dict)
