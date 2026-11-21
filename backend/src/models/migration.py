from sqlalchemy import Column,Integer,String,Text
from ..config.database import Base
class SchemaMigration(Base):
    """轻量迁移台账：每个版本只执行一次，中断重启后不重复执行。"""
    __tablename__='schema_migrations'
    id=Column(Integer,primary_key=True)
    version=Column(String(80),unique=True,nullable=False)
    applied_at=Column(String(40),nullable=False)
    detail=Column(Text,default='')
