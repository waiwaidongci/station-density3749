import json,time,logging,socket,uuid
from datetime import datetime
from redis import Redis
from sqlalchemy import select
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit,migration,quarantine
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
from .services.migrations import run_migrations
from .utils.serialization import now
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
WORKER_ID=f'{socket.gethostname()}-{uuid.uuid4().hex[:8]}'
# 租约超时：超过该时长没有心跳的 running 任务视为中断，可安全重投
LEASE_TIMEOUT_SECONDS=120
def _parse_ts(value):
    try:return datetime.fromisoformat(value)
    except Exception:return None
def recover_queued_runs(db):
    """启动恢复：把所有 queued 任务重新入队。

    Redis 队列可能因切换/重启而丢失，但任务状态在数据库中持久化。重复入队安全：
    execute 的状态闸门只领取 queued，completed/running 任务会被直接跳过，不会重复计算。
    """
    ids=[int(x) for (x,) in db.execute(select(Run.id).where(Run.state=='queued')).all()]
    for rid in ids:
        try:queue.rpush('analysis:queue',rid)
        except Exception:logging.exception('startup requeue run %s failed',rid)
    if ids:log(db,'worker','run.recovered',','.join(map(str,ids)),f'worker {WORKER_ID} 启动恢复 queued 任务')
    db.commit()
    return ids
def reap_stale_runs(db):
    """回收中断任务：租约过期的 running 任务重置为 queued 并重新入队。

    结果与 CSV 只在计算成功后一次性提交，因此重投不会产生重复结果或部分写入。
    """
    cutoff=time.time()-LEASE_TIMEOUT_SECONDS
    stale=[]
    for r in db.scalars(select(Run).where(Run.state=='running')):
        hb=_parse_ts(r.heartbeat_at) if r.heartbeat_at else None
        anchor=hb.timestamp() if hb else (_parse_ts(r.leased_at).timestamp() if r.leased_at else 0)
        if anchor and anchor<cutoff:
            r.state='queued';r.leased_by=None;r.leased_at=None;r.heartbeat_at=None
            stale.append(r.id)
    db.commit()
    for rid in stale:
        try:queue.rpush('analysis:queue',rid)
        except Exception:logging.exception('requeue stale run %s failed',rid)
        log(db,'worker','run.requeued',rid,f'worker {WORKER_ID} 回收中断任务并重新入队')
    db.commit()
    return stale
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        # 幂等闸门：只有 queued 可领取；completed 不重复计算，running 被其他租约持有则跳过
        if not r or r.state!='queued':return
        r.state='running';r.leased_by=WORKER_ID;r.leased_at=now();r.heartbeat_at=now()
        r.attempts=(r.attempts or 0)+1
        db.commit()
    stop=None
    try:
        # 长计算期间的租约心跳由独立线程维持，中断后其他 worker 可凭过期租约回收
        stop=heartbeat(id)
        with Session() as db:
            r=db.get(Run,id)
            result=calculate(r.snapshot['records'],r.parameters)
            json.dumps(result,allow_nan=False)
            response=csv_response(result,'run.csv')
            # 先落结果再写 CSV（CSV 由结果确定性派生），整体成功后才置 completed
            r.result=result;r.state='completed';r.error='';r.heartbeat_at=now()
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id,
                f"worker {WORKER_ID} 第 {r.attempts} 次尝试完成；输入版本 {','.join(r.snapshot.get('record_unit_versions',[]))}")
            db.commit()
    except Exception as e:
        logging.exception('analysis failed')
        with Session() as db:
            r=db.get(Run,id)
            if r and r.state=='running':
                r.state='failed';r.error=str(e)[:2000];r.heartbeat_at=now()
                log(db,'worker','run.failed',r.id,r.error)
                db.commit()
    finally:
        if stop is not None:stop.set()
def heartbeat(run_id):
    import threading
    stop=threading.Event()
    def tick():
        while not stop.wait(30):
            try:
                with Session() as db:
                    r=db.get(Run,run_id)
                    if r and r.state=='running':
                        r.heartbeat_at=now();db.commit()
            except Exception:logging.exception('heartbeat failed')
    threading.Thread(target=tick,daemon=True).start()
    return stop
def main():
    run_migrations()
    with Session() as db:recover_queued_runs(db)
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        try:
            with Session() as db:reap_stale_runs(db)
        except Exception:logging.exception('reap cycle failed')
        value=queue.blpop('analysis:queue',timeout=2)
        if value:execute(int(value[1]))
if __name__=='__main__':main()
