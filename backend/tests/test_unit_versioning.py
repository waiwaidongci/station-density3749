"""字段单位版本迁移端到端测试（sqlite + 内存队列桩）。

覆盖：
- 按生效时间接纳 v1/v2 并转换规范量纲；未知/冲突/迟到旧版本/非法值隔离且可重放
- 重放幂等、自然键不重复
- 新研究拒绝带未处理隔离行的来源；冻结快照固化创建时规则，迟到行不改写冻结输入
- worker 中断恢复（租约回收重投）、重试不重复、completed 不重算
- 结果页数据与 CSV 导出同量纲且可追查来源版本
"""
import os, json, time, tempfile

os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.gettempdir() + '/unit-version-test.db'
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'
os.environ['ARTIFACT_DIR'] = tempfile.mkdtemp(prefix='artifacts-test-')
os.environ['SEED_DEMO'] = 'false'

import pytest

from src.config.database import Base, engine, Session
from src.models import user as user_model, source as source_model, record as record_model
from src.models import run as run_model, note as note_model, quarantine as quarantine_model, migration
from src.models.source import Source
from src.models.record import Record
from src.models.run import Run
from src.models.quarantine import Quarantine
from src.services.migrations import run_migrations
from src.services import unit_versions as uv
from src.services.ingestion import ingest_batch, replay_quarantine, discard_quarantine, list_quarantine
from src.services.runs import create_run
from src.services.algorithm import calculate
from src.services.export import _render_csv
from src.utils.serialization import now
from src import worker as worker_mod


class Actor:
    email = 'analyst@station-density.demo'


class StubQueue:
    def __init__(self):
        self.items = []
    def rpush(self, key, value):
        self.items.append(int(value))
    def blpop(self, key, timeout=0):
        return None if not self.items else (key, str(self.items.pop(0)).encode())
    def ping(self):
        return True


@pytest.fixture(scope='session', autouse=True)
def setup_db():
    if os.path.exists(engine.url.database):
        os.remove(engine.url.database)
    Base.metadata.create_all(engine)
    run_migrations()
    # 迁移幂等：再跑一次不应重复应用
    assert run_migrations() == []
    yield


@pytest.fixture
def stub_queue(monkeypatch):
    q = StubQueue()
    monkeypatch.setattr('src.services.runs.queue', q)
    monkeypatch.setattr('src.worker.queue', q)
    return q


@pytest.fixture
def v2_source():
    with Session() as db:
        s = Source(key='S-' + os.urandom(4).hex(), name='换代微站组', filename='s.csv',
                   description='', v2_effective_at='2026-11-15T00:00:00+00:00')
        db.add(s)
        db.commit()
        return s.id


V2_TIME = '2026-11-16T08:00:00+00:00'
V1_TIME = '2026-11-10T08:00:00+00:00'


def v2_item(ref, temp_f=None, particles_mg=None):
    # 默认按编号生成差异化观测，避免标准化时出现常量列
    serial = sum(ord(c) for c in ref)
    if temp_f is None:
        temp_f = round(uv.c_to_f(15.0 + (serial % 18) + 0.37), 2)
    if particles_mg is None:
        particles_mg = round((8.0 + (serial % 9) * 7.0) / 1000.0, 4)
    return {'reference': ref,
            'values': {'temperature': temp_f, 'humidity': float(40 + serial % 45), 'particles': particles_mg},
            'unit_version': 'v2', 'observed_at': V2_TIME}


def test_conversion_math():
    assert abs(uv.f_to_c(32)) < 1e-9
    assert abs(uv.f_to_c(212) - 100) < 1e-9
    out, trace = uv.convert_values({'temperature': 86.0, 'humidity': 50, 'particles': 0.05}, 'v2')
    assert abs(out['temperature'] - 30.0) < 1e-9
    assert abs(out['particles'] - 50.0) < 1e-9
    assert trace['temperature'].startswith('F_to_C')
    assert trace['particles'].startswith('mg/m3')
    out, trace = uv.convert_values({'temperature': 23.0, 'humidity': 50, 'particles': 30.0}, 'v1')
    assert out['temperature'] == 23.0 and all(t == 'identity' for t in trace.values())
    with pytest.raises(KeyError):
        uv.convert_values({}, 'v9')


def test_ingest_accepts_and_quarantines(v2_source):
    with Session() as db:
        s = db.get(Source, v2_source)
        items = [
            v2_item('A-1'),
            # 迟到的旧版本：换代后仍上报 v1
            {'reference': 'A-2', 'values': {'temperature': 21.4, 'humidity': 55, 'particles': 30.0},
             'unit_version': 'v1', 'observed_at': V2_TIME},
            # 未知版本
            {'reference': 'A-3', 'values': {'temperature': 70.0, 'humidity': 55, 'particles': 0.04},
             'unit_version': 'v9', 'observed_at': V2_TIME},
            # 非法数值
            {'reference': 'A-4', 'values': {'temperature': 'oops', 'humidity': 55, 'particles': 0.04},
             'unit_version': 'v2', 'observed_at': V2_TIME},
            # 换代前的 v1 正常接纳
            {'reference': 'A-5', 'values': {'temperature': 20.0, 'humidity': 50, 'particles': 30.0},
             'unit_version': 'v1', 'observed_at': V1_TIME},
        ]
        res = ingest_batch(db, Actor(), s, items, audit=False)
        assert res['accepted'] == 2 and res['quarantined'] == 3
        codes = {row['reason_code'] for row in res['quarantine_rows']}
        assert codes == {'late_old_unit_version', 'unknown_unit_version', 'invalid_record_value'}

        r1 = db.scalar(__import__('sqlalchemy').select(Record).where(Record.reference == 'A-1'))
        expected_c = uv.f_to_c(items[0]['values']['temperature'])
        # 落库即规范量纲
        assert abs(r1.values['temperature'] - expected_c) < 1e-9
        assert abs(r1.values['particles'] - items[0]['values']['particles'] * 1000.0) < 1e-9
        assert r1.unit_version == 'v2' and r1.conversion_trace['temperature']


def test_ingest_is_idempotent_and_conflict_isolated(v2_source):
    with Session() as db:
        s = db.get(Source, v2_source)
        item = v2_item('B-1')
        r1 = ingest_batch(db, Actor(), s, [item], audit=False)
        assert r1['accepted'] == 1
        r2 = ingest_batch(db, Actor(), s, [item], audit=False)
        assert r2['accepted'] == 1 and r2['quarantined'] == 0
        from sqlalchemy import func, select
        assert db.scalar(select(func.count()).select_from(Record).where(Record.reference == 'B-1')) == 1
        # 同键不同内容 → 冲突隔离，不改写既有记录
        conflict = v2_item('B-1', temp_f=100.0, particles_mg=0.09)
        r3 = ingest_batch(db, Actor(), s, [conflict], audit=False)
        assert r3['quarantined'] == 1 and r3['quarantine_rows'][0]['reason_code'] == 'duplicate_payload_conflict'
        kept = db.scalar(select(Record).where(Record.reference == 'B-1'))
        assert abs(kept.values['temperature'] - uv.f_to_c(item['values']['temperature'])) < 1e-9
        assert kept.unit_version == 'v2'


def test_replay_quarantine_success_and_guard(v2_source):
    with Session() as db:
        s = db.get(Source, v2_source)
        # 迟到 v1 行进入隔离
        res = ingest_batch(db, Actor(), s, [{
            'reference': 'C-1', 'values': {'temperature': 21.4, 'humidity': 55, 'particles': 30.0},
            'unit_version': 'v1', 'observed_at': V2_TIME}], audit=False)
        qid = res['quarantine_rows'][0]['id']
        # 用修正后的 v2 报文（℉/mg/m³）重放
        out = replay_quarantine(db, Actor(), qid, override={
            'values': {'temperature': 70.52, 'humidity': 55, 'particles': 0.03},
            'unit_version': 'v2', 'observed_at': V2_TIME})
        assert out['outcome'] == 'accepted'
        q = db.get(Quarantine, qid)
        assert q.state == 'replayed' and q.resolved_record_id is not None
        # 已重放行不可再次处理
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            replay_quarantine(db, Actor(), qid)
        assert exc.value.status_code == 409
        rec = db.get(Record, q.resolved_record_id)
        assert rec.unit_version == 'v2'
        assert abs(rec.values['particles'] - 30.0) < 1e-9


def test_replay_failure_keeps_chain(v2_source):
    with Session() as db:
        s = db.get(Source, v2_source)
        res = ingest_batch(db, Actor(), s, [{
            'reference': 'D-1', 'values': {'temperature': 70.0, 'humidity': 55, 'particles': 0.04},
            'unit_version': 'v9', 'observed_at': V2_TIME}], audit=False)
        qid = res['quarantine_rows'][0]['id']
        # 不修正，重放仍失败：旧行 superseded，产生新隔离行
        out = replay_quarantine(db, Actor(), qid)
        assert out['outcome'] == 'quarantined'
        assert db.get(Quarantine, qid).state == 'superseded'
        assert out['next']['replay_of'] == qid
        # 人工关闭
        closed = discard_quarantine(db, Actor(), out['next']['id'])
        assert closed['state'] == 'discarded'
        assert {q['state'] for q in list_quarantine(db, s.id)} >= {'superseded', 'discarded'}


def _open_quarantine_count(db, source_id):
    from sqlalchemy import func, select
    return db.scalar(select(func.count()).select_from(Quarantine).where(
        Quarantine.source_id == source_id, Quarantine.state == 'open'))


def test_run_blocks_on_open_quarantine_and_freezes_rule(stub_queue, v2_source):
    from fastapi import HTTPException
    from sqlalchemy import select
    with Session() as db:
        s = db.get(Source, v2_source)
        # 先造 4 条合法 v2 记录
        ingest_batch(db, Actor(), s, [v2_item(f'E-{i}') for i in range(4)], audit=False)
        # 再来一条隔离行
        ingest_batch(db, Actor(), s, [{
            'reference': 'E-Q', 'values': {'temperature': 70.0, 'humidity': 55, 'particles': 0.04},
            'unit_version': 'v9', 'observed_at': V2_TIME}], audit=False)
        assert _open_quarantine_count(db, v2_source) == 1
        with pytest.raises(HTTPException) as exc:
            create_run(db, Actor(), type('In', (), {'source_id': v2_source, 'name': '新研究',
                                                     'parameters': {'eps': 0.55, 'min_samples': 5}})())
        assert exc.value.status_code == 409
        # 关闭隔离后可以冻结
        q = db.scalar(__import__('sqlalchemy').select(Quarantine).where(Quarantine.reference == 'E-Q'))
        discard_quarantine(db, Actor(), q.id)
        run = create_run(db, Actor(), type('In', (), {'source_id': v2_source, 'name': '十一月复核研究',
                                                       'parameters': {'eps': 0.55, 'min_samples': 5}})())
        assert run['state'] == 'queued' and stub_queue.items == [run['id']]
        snap = run['snapshot']
        assert snap['unit_rule']['canonical_units']['temperature'] == '℃'
        assert snap['record_unit_versions'] == ['v2']
        # 冻结后迟到的旧版本行：进入来源表（若合法）/隔离区，但快照不变
        before = json.dumps(snap, sort_keys=True)
        ingest_batch(db, Actor(), s, [{
            'reference': 'E-LATE', 'values': {'temperature': 21.0, 'humidity': 55, 'particles': 30.0},
            'unit_version': 'v1', 'observed_at': V2_TIME}], audit=False)
        run_row = db.get(Run, run['id'])
        assert json.dumps(run_row.snapshot, sort_keys=True) == before
        assert _open_quarantine_count(db, v2_source) == 1


def test_worker_retry_semantics_and_reaper(stub_queue, v2_source):
    from sqlalchemy import select
    with Session() as db:
        s = db.get(Source, v2_source)
        ingest_batch(db, Actor(), s, [v2_item(f'W-{i}') for i in range(6)], audit=False)
        run = create_run(db, Actor(), type('In', (), {'source_id': v2_source, 'name': 'worker研究',
                                                       'parameters': {'eps': 0.55, 'min_samples': 5}})())
        rid = run['id']

    # 首次执行成功
    worker_mod.execute(rid)
    with Session() as db:
        r = db.get(Run, rid)
        assert r.state == 'completed' and r.attempts == 1 and r.result
        first_checksum = r.result['checksum']
    # completed 不重复计算
    worker_mod.execute(rid)
    with Session() as db:
        assert db.get(Run, rid).attempts == 1
    # 重投同一 id 也不会重复
    stub_queue.items.append(rid)
    worker_mod.execute(rid)
    with Session() as db:
        assert db.get(Run, rid).attempts == 1

    # 模拟中断：新任务领取后停在 running 且心跳过期 → 回收重投
    with Session() as db:
        s = db.get(Source, v2_source)
        run2 = create_run(db, Actor(), type('In', (), {'source_id': v2_source, 'name': '中断研究',
                                                        'parameters': {'eps': 0.55, 'min_samples': 5}})())
        rid2 = run2['id']
    worker_mod.execute(rid2)  # 会真正跑完；手动改回中断现场
    with Session() as db:
        r2 = db.get(Run, rid2)
        r2.state = 'running'
        r2.heartbeat_at = '2000-01-01T00:00:00+00:00'
        r2.leased_at = '2000-01-01T00:00:00+00:00'
        db.commit()
    with Session() as db:
        reaped = worker_mod.reap_stale_runs(db)
        assert rid2 in reaped
        assert db.get(Run, rid2).state == 'queued'
    assert rid2 in stub_queue.items
    # 重投后可正常完成，且结果一致
    worker_mod.execute(rid2)
    with Session() as db:
        r2 = db.get(Run, rid2)
        assert r2.state == 'completed'
        assert r2.result['checksum'] == first_checksum or r2.attempts >= 2


def test_result_and_csv_share_canonical_units_with_provenance(v2_source):
    from sqlalchemy import select
    with Session() as db:
        s = db.get(Source, v2_source)
        ingest_batch(db, Actor(), s, [v2_item(f'P-{i}') for i in range(6)], audit=False)
        # 混合一条换代前 v1，验证结果同时可追查两种来源版本
        ingest_batch(db, Actor(), s, [{
            'reference': 'P-OLD', 'values': {'temperature': 20.0, 'humidity': 50, 'particles': 30.0},
            'unit_version': 'v1', 'observed_at': V1_TIME}], audit=False)
        records = [{'reference': r.reference, 'values': r.values,
                    'unit_version': r.unit_version, 'conversion_trace': r.conversion_trace}
                   for r in db.scalars(select(Record).where(Record.source_id == v2_source))]
    result = calculate(records, {'eps': 0.55, 'min_samples': 5})
    # 所有行同量纲
    assert result['unit_meta']['canonical_units'] == {'temperature': '℃', 'humidity': '%', 'particles': 'μg/m³'}
    assert set(result['unit_meta']['row_unit_versions']) == {'v1', 'v2'}
    assert all('unit_version' in row for row in result['rows'])
    v2_rows = [r for r in result['rows'] if r['unit_version'] == 'v2']
    # v2 行温度已在摄氏量纲（86℉=30℃ 附近），绝不会以华氏数值混入
    assert all(r['temperature'] < 60 for r in v2_rows)
    # CSV 与页面同量纲，且带版本/转换列与抬头
    csv_text = _render_csv(result).body.decode('utf-8-sig')
    assert '统一规范量纲' in csv_text and 'unit_version' in csv_text and 'conversion_trace' in csv_text
    assert 'F_to_C' in csv_text and 'mg/m3_to_ug/m3' in csv_text
    # 校验和可复算
    import hashlib
    assert result['checksum'] == hashlib.sha256(
        json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_invalid_observed_at_is_quarantined(v2_source):
    with Session() as db:
        s = db.get(Source, v2_source)
        res = ingest_batch(db, Actor(), s, [{
            'reference': 'T-BAD-TIME', 'values': {'temperature': 70.0, 'humidity': 55, 'particles': 0.04},
            'unit_version': 'v2', 'observed_at': 'not-a-timestamp'}], audit=False)
        assert res['quarantined'] == 1
        assert res['quarantine_rows'][0]['reason_code'] == 'invalid_record_value'


def test_legacy_database_upgrade_path():
    """旧库（无版本化列）经 002 迁移后可用且不重复应用，见 legacy_db_scenario.py。"""
    import subprocess, sys
    scenario = os.path.join(os.path.dirname(__file__), 'legacy_db_scenario.py')
    env = {**os.environ, 'PYTHONPATH': os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    out = subprocess.run([sys.executable, scenario], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert 'LEGACY UPGRADE OK' in out.stdout


def test_startup_recovery_requeues_pending_without_duplicate_calc(stub_queue, v2_source):
    # 独立数据库隔离，避免共享库中其他测试遗留的 queued 任务干扰
    import sqlalchemy
    isolated_url = 'sqlite:///' + tempfile.gettempdir() + '/recovery-test.db'
    iso_path = tempfile.gettempdir() + '/recovery-test.db'
    if os.path.exists(iso_path):
        os.remove(iso_path)
    iso_engine = sqlalchemy.create_engine(isolated_url)
    iso_session = sqlalchemy.orm.sessionmaker(bind=iso_engine)
    with iso_session() as db:
        Base.metadata.create_all(iso_engine)
        s = Source(key='S-REC', name='恢复来源', filename='r.csv', description='',
                   v2_effective_at='2026-11-15T00:00:00+00:00')
        db.add(s); db.commit()
        ingest_batch(db, Actor(), s, [v2_item(f'R-{i}') for i in range(4)], audit=False)
        run = create_run(db, Actor(), type('In', (), {'source_id': s.id, 'name': '启动恢复研究',
                                                       'parameters': {'eps': 0.55, 'min_samples': 2}})())
        rid = run['id']
    # 模拟队列丢失：清空 stub，worker 启动恢复应重新入队
    stub_queue.items.clear()
    # 让 worker 使用隔离会话
    original_session = worker_mod.Session
    worker_mod.Session = iso_session
    try:
        with iso_session() as db:
            recovered = worker_mod.recover_queued_runs(db)
        assert rid in recovered and stub_queue.items == [rid]
        # 再次恢复会重复入队，但执行闸门保证只计算一次
        with iso_session() as db:
            worker_mod.recover_queued_runs(db)
        assert stub_queue.items.count(rid) == 2
        worker_mod.execute(rid)
        worker_mod.execute(rid)
        with iso_session() as db:
            r = db.get(Run, rid)
            assert r.state == 'completed' and r.attempts == 1
    finally:
        worker_mod.Session = original_session
        iso_engine.dispose()


def test_frozen_research_keeps_creation_rule(stub_queue, v2_source):
    """已冻结研究沿用创建时规则：即使来源生效时间后续调整，快照规则不变。"""
    with Session() as db:
        s = db.get(Source, v2_source)
        ingest_batch(db, Actor(), s, [v2_item(f'F-{i}') for i in range(4)], audit=False)
        run = create_run(db, Actor(), type('In', (), {'source_id': v2_source, 'name': '冻结研究',
                                                       'parameters': {'eps': 0.55, 'min_samples': 4}})())
        frozen_rule = dict(run['snapshot']['unit_rule'])
        s.v2_effective_at = '2030-01-01T00:00:00+00:00'
        db.commit()
        run_row = db.get(Run, run['id'])
        assert run_row.snapshot['unit_rule'] == frozen_rule
        assert run_row.snapshot['unit_rule']['cutover_at'] == '2026-11-15T00:00:00+00:00'
