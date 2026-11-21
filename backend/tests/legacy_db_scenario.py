"""旧库升级场景：在仅有 001 旧表结构的数据库上执行 002 迁移。

由 test_unit_versioning.py 以子进程调用，独立 DATABASE_URL，模拟已上线卷的升级：
旧表无 unit_version/租约等列 → create_all 补新表 → 002 幂等 ALTER 补列并回填 v1。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

db_path = os.path.join(tempfile.gettempdir(), 'legacy-upgrade-test.db')
if os.path.exists(db_path):
    os.remove(db_path)
os.environ['DATABASE_URL'] = 'sqlite:///' + db_path
os.environ['ARTIFACT_DIR'] = tempfile.mkdtemp(prefix='legacy-art-')
os.environ['SEED_DEMO'] = 'false'

from sqlalchemy import create_engine, text

legacy = create_engine(os.environ['DATABASE_URL'])
with legacy.begin() as conn:
    # 复刻 001 初始结构（不含任何版本化/租约列）
    conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(120) NOT NULL, "
                      "password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL)"))
    conn.execute(text("CREATE TABLE surveys (id INTEGER PRIMARY KEY, key VARCHAR(120) NOT NULL UNIQUE, "
                      "name VARCHAR(160) NOT NULL, filename VARCHAR(160) NOT NULL, description TEXT)"))
    conn.execute(text("CREATE TABLE station_readings (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, "
                      "reference VARCHAR(120) NOT NULL, \"values\" JSON NOT NULL, "
                      "UNIQUE(source_id, reference))"))
    conn.execute(text("CREATE TABLE density_runs (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, "
                      "name VARCHAR(160) NOT NULL, state VARCHAR(20), parameters JSON NOT NULL, "
                      "snapshot JSON NOT NULL, result JSON, error TEXT, created_at VARCHAR(40) NOT NULL)"))
    conn.execute(text("CREATE TABLE visit_plans (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
                      "title VARCHAR(160) NOT NULL, body TEXT NOT NULL, state VARCHAR(20), "
                      "frozen_result JSON NOT NULL, author VARCHAR(120) NOT NULL, approved_by VARCHAR(120))"))
    conn.execute(text("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, actor VARCHAR(120) NOT NULL, "
                      "action VARCHAR(80) NOT NULL, target VARCHAR(160) NOT NULL, detail TEXT, "
                      "created_at VARCHAR(40) NOT NULL)"))
    import json
    conn.execute(text("INSERT INTO surveys (id,key,name,filename,description) VALUES "
                      "(1,'HL-OLD','旧来源','old.csv','legacy')"))
    for i in range(4):
        vals = json.dumps({'temperature': 20.0 + i, 'humidity': 50 + i, 'particles': 30.0 + i})
        conn.execute(text("INSERT INTO station_readings (source_id,reference,\"values\") VALUES "
                          "(1,:r,:v)"), {'r': f'OLD-{i}', 'v': vals})
    conn.execute(text("INSERT INTO density_runs (id,source_id,name,state,parameters,snapshot,created_at) "
                      "VALUES (1,1,'旧研究','queued',:p,:s,'2026-11-01T00:00:00+00:00')"),
                 {'p': json.dumps({'eps': 0.55, 'min_samples': 2}),
                  's': json.dumps({'source': {}, 'records': []})})
legacy.dispose()

# 执行应用启动时的同一迁移序列
from src.config.database import Base, engine, Session
from src.models import user, source, record, run, note, audit, quarantine, migration
from src.services.migrations import run_migrations

Base.metadata.create_all(engine)
applied = run_migrations()
assert applied == ['002_unit_versioning'], applied
assert run_migrations() == []  # 重启后不重复应用

from src.models.record import Record
from src.models.run import Run
from src.models.quarantine import Quarantine
from src.models.migration import SchemaMigration
from sqlalchemy import select, func

with Session() as db:
    # 历史记录沿用创建时规则：回填 v1、空转换痕迹
    rows = db.scalars(select(Record)).all()
    assert len(rows) == 4
    assert all(r.unit_version == 'v1' for r in rows)
    assert all(r.conversion_trace == {} for r in rows)
    # 旧任务获得租约列默认值，且不被自动重算
    r = db.get(Run, 1)
    assert r.attempts == 0 and r.leased_by is None and r.state == 'queued'
    # 新表存在且迁移已登记
    assert db.scalar(select(func.count()).select_from(SchemaMigration)) == 1
    assert db.scalar(select(func.count()).select_from(Quarantine)) == 0

# 迁移后的旧数据可直接进入算法（规范量纲）
from src.services.algorithm import calculate
with Session() as db:
    recs = [{'reference': r.reference, 'values': r.values,
             'unit_version': r.unit_version, 'conversion_trace': r.conversion_trace}
            for r in db.scalars(select(Record))]
result = calculate(recs, {'eps': 0.55, 'min_samples': 2})
assert result['unit_meta']['canonical_units']['temperature'] == '℃'
assert set(result['unit_meta']['row_unit_versions']) == {'v1'}
print('LEGACY UPGRADE OK')
