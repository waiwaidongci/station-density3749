from sqlalchemy import Column,Integer,String,Text
from ..config.database import Base
from ..config.settings import CONFIG
class Source(Base):
    __tablename__=CONFIG['source_table']
    id=Column(Integer,primary_key=True)
    key=Column(String(120),unique=True,nullable=False)
    name=Column(String(160),nullable=False)
    filename=Column(String(160),nullable=False)
    description=Column(Text,default='')
    # 该来源传感器换代、v2 单位开始生效的 UTC 时间；为空则使用全局生效时间
    v2_effective_at=Column(String(40),nullable=True)
