from pydantic import BaseModel,Field
class SourceInput(BaseModel):
    name:str=Field(min_length=2,max_length=160)
    filename:str=Field(min_length=3,max_length=160)
    description:str=Field(default='',max_length=2000)
    # 可选：该来源 v2 单位生效时间（UTC ISO8601）；不传则沿用全局生效时间
    v2_effective_at:str|None=Field(default=None,max_length=40)
class RecordInput(BaseModel):
    reference:str=Field(min_length=1,max_length=120)
    values:dict
    # 上报单位版本 v1/v2；缺省按 v1 处理并经生效时间核对
    unit_version:str|None=Field(default=None,max_length=20)
    # 观测时间 UTC ISO8601；用于按生效时间接纳版本
    observed_at:str|None=Field(default=None,max_length=40)
class RecordBatch(BaseModel):
    records:list[RecordInput]=Field(min_length=1,max_length=1000)
class ReplayInput(BaseModel):
    # 可选修正项；不传则沿用隔离行原始报文重试
    values:dict|None=None
    unit_version:str|None=Field(default=None,max_length=20)
    observed_at:str|None=Field(default=None,max_length=40)
