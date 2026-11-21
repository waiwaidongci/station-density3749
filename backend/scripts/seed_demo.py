import json,random,logging
from sqlalchemy import select,func
from src.config.database import Base,engine,Session
from src.config.settings import CONFIG
from src.models.user import User
from src.models.source import Source
from src.models.record import Record
from src.models.run import Run
from src.models import note,audit,quarantine,migration
from src.utils.security import hash_password
from src.utils.serialization import now
from src.services.runs import queue
from src.services.domain_validation import validate_record
from src.services import unit_versions as uv
from src.services.ingestion import ingest_batch

V1_OBSERVED='2026-11-10T08:00:00+00:00'
V2_OBSERVED='2026-11-16T08:00:00+00:00'

class SeedActor:
    email='seed@system'

def seed_values(kind,index,batch,rng):
    if kind=='gaussian':
        cls=index%3;return {'rms':round(max(.1,rng.gauss([1.2,3.0,2.0][cls]+batch*.08,.35)),3),'centroid':round(max(100,rng.gauss([900,1600,2300][cls],170)),2),'kurtosis':round(max(.1,rng.gauss([2.8,5.2,3.9][cls],.45)),3),'label':['正常','轴承磨损','转子偏心'][cls]}
    if kind=='density':
        cls=index%3
        if index%19==0:return {'temperature':round(rng.uniform(12,42),2),'humidity':round(rng.uniform(12,92),2),'particles':round(rng.uniform(4,140),2)}
        return {'temperature':round(rng.gauss([19,26,32][cls]+batch*.3,.8),2),'humidity':round(rng.gauss([72,45,28][cls],2.5),2),'particles':round(max(.1,rng.gauss([22,48,85][cls],3)),2)}
    if kind=='transition':
        choices=[['序章','材料','工艺','案例'],['序章','案例','附录'],['材料','工艺','案例','附录'],['序章','材料','案例'],['序章']]
        path=choices[rng.randrange(len(choices))];return {'session':f'SY-{batch+1}-{index+1:04}','path':'>'.join(path)}
    if kind=='forest':
        moisture=rng.uniform(20,60);thickness=rng.uniform(1,8);temperature=rng.uniform(50,100);airflow=rng.uniform(1,5)
        minutes=12+1.6*moisture+8*thickness-.45*temperature-3*airflow+0.1*moisture*thickness+rng.gauss(0,3)
        return {'moisture':round(moisture,2),'thickness':round(thickness,2),'temperature':round(temperature,2),'airflow':round(airflow,2),'minutes':round(max(5,minutes),2)}
    if kind=='neighbors':
        uid=index//6;slot=index%6;exhibit=(uid%3*3+slot)%12
        return {'visitor':f'V{uid+1:03}','exhibit':f'E{exhibit+1:02}','rating':round(max(1,min(5,3+(1 if exhibit%3==uid%3 else -.4)+rng.gauss(0,.55))),1)}

def counts(db):
    return {cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in (User,Source,Record,Run,quarantine.Quarantine)}

def seed():
    Base.metadata.create_all(engine);rng=random.Random(42);queued=[]
    with Session() as db:
        for role in ('admin','analyst','viewer'):
            email=role+'@'+CONFIG['slug']+'.demo'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password_hash=hash_password('Research2026!'),role=role))
        for batch in range(3):
            key=CONFIG['code']+f'-202611-{batch+1:02}'
            source=db.scalar(select(Source).where(Source.key==key))
            if not source:
                source=Source(key=key,name=key+' '+['秋季常规组','复测对照组','工艺观察组'][batch],filename=CONFIG['slug']+f'-202611-{batch+1:02}.csv',description=CONFIG['scenario'],v2_effective_at=None);db.add(source);db.flush()
            for index in range(CONFIG['seed_count']):
                reference=key+f'-{index+1:04}'
                vals=seed_values(CONFIG['algorithm'],index,batch,rng)
                if not db.scalar(select(Record.id).where(Record.source_id==source.id,Record.reference==reference)):
                    # 历史记录沿用创建时规则：原始单位即规范量纲，标记 v1、无转换
                    db.add(Record(source_id=source.id,reference=reference,values=validate_record(vals),
                                  unit_version='v1',observed_at=V1_OBSERVED,conversion_trace={}))
            db.flush()
            if batch==0 and not db.scalar(select(Run.id).where(Run.name=='首轮种子研究')):
                records=[{'id':r.id,'source_id':r.source_id,'reference':r.reference,'values':r.values,
                          'unit_version':r.unit_version,'observed_at':r.observed_at,'conversion_trace':r.conversion_trace}
                         for r in db.scalars(select(Record).where(Record.source_id==source.id).order_by(Record.id))]
                run=Run(source_id=source.id,name='首轮种子研究',state='queued',parameters=CONFIG['default_parameters'],
                        snapshot={'source':{'id':source.id,'name':source.name,'filename':source.filename},
                                  'records':records,
                                  'unit_rule':{'canonical_units':dict(uv.CANONICAL_UNITS),'cutover_at':uv.CUTOVER_AT},
                                  'frozen_at':now(),'record_unit_versions':['v1']},
                        created_at=now(),attempts=0);db.add(run);db.flush();queued.append(run.id)

        # 换代微站：v2 来源，上报 ℉ 与 mg/m³，系统接纳时转换为 ℃、μg/m³
        v2_key=CONFIG['code']+'-202611-04'
        v2_source=db.scalar(select(Source).where(Source.key==v2_key))
        if not v2_source:
            v2_source=Source(key=v2_key,name=v2_key+' 换代传感器组',filename=CONFIG['slug']+'-202611-04.csv',
                             description='传感器换代后以华氏温度与 mg/m³ 颗粒浓度上报，按生效时间接纳 v2 并统一转换',
                             v2_effective_at=CONFIG['unit_cutover_at']);db.add(v2_source);db.flush()
        existing_v2=db.scalar(select(func.count()).select_from(Record).where(Record.source_id==v2_source.id))
        if not existing_v2:
            items=[]
            for index in range(60):
                canonical=seed_values(CONFIG['algorithm'],index,0,rng)
                # 以 v2 原始单位构造上报报文：℃→℉，μg/m³→mg/m³
                items.append({'reference':f'{v2_key}-{index+1:04}',
                              'values':{'temperature':round(uv.c_to_f(canonical['temperature']),2),
                                        'humidity':canonical['humidity'],
                                        'particles':round(canonical['particles']/1000.0,4)},
                              'unit_version':'v2','observed_at':V2_OBSERVED})
            # 同时构造三类典型隔离行：未知版本、迟到旧版本、非法数值
            items.append({'reference':f'{v2_key}-Q-001','values':{'temperature':70.1,'humidity':55,'particles':0.03},
                          'unit_version':'v9','observed_at':V2_OBSERVED})
            items.append({'reference':f'{v2_key}-Q-002','values':{'temperature':21.4,'humidity':55,'particles':30.0},
                          'unit_version':'v1','observed_at':V2_OBSERVED})
            items.append({'reference':f'{v2_key}-Q-003','values':{'temperature':'NaN-text','humidity':55,'particles':0.03},
                          'unit_version':'v2','observed_at':V2_OBSERVED})
            ingest_batch(db,SeedActor(),v2_source,items,audit=False)
        db.commit();print(json.dumps(counts(db),ensure_ascii=False))
    for id in queued:
        try:queue.rpush('analysis:queue',id)
        except Exception:logging.exception('seed queue unavailable')
if __name__=='__main__':seed()
