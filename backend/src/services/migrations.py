"""数据库结构迁移：轻量台账 + 方言自适应 ALTER。

- SQLAlchemy create_all 负责新建表；对已存在表的新增列由这里幂等补齐。
- schema_migrations 记录已应用版本，切换/启动中断后重跑不重复执行。
- 每条迁移在同一事务内登记，失败回滚后下次启动可再次尝试。
"""
import logging
from sqlalchemy import text
from ..config.database import engine
from ..config.settings import CONFIG
from ..models.migration import SchemaMigration
from ..utils.serialization import now


def _column_exists(conn, table, column):
    if engine.dialect.name == 'sqlite':
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    rows = conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
        {'t': table, 'c': column},
    ).fetchall()
    return bool(rows)


def _add_column(conn, table, column, ddl_type, default_ddl=None, nullable=True):
    if _column_exists(conn, table, column):
        return False
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
    if not nullable:
        ddl += ' NOT NULL'
    if default_ddl is not None:
        ddl += f" DEFAULT {default_ddl}"
    conn.execute(text(ddl))
    return True


MIGRATIONS = [
    (
        '002_unit_versioning',
        '字段单位版本迁移：记录版本/观测时间/转换痕迹，来源生效时间，任务租约',
        lambda conn: _migrate_002(conn),
    ),
]


def _migrate_002(conn):
    record_table = CONFIG['record_table']
    source_table = CONFIG['source_table']
    run_table = CONFIG['run_table']
    default_version = CONFIG['default_unit_version']
    changed = []
    if _add_column(conn, record_table, 'unit_version', 'VARCHAR(20)', f"'{default_version}'", False):
        changed.append(record_table + '.unit_version')
    if _add_column(conn, record_table, 'observed_at', 'VARCHAR(40)'):
        changed.append(record_table + '.observed_at')
    if _add_column(conn, record_table, 'conversion_trace', 'JSON', "'{}'"):
        changed.append(record_table + '.conversion_trace')
    if _add_column(conn, source_table, 'v2_effective_at', 'VARCHAR(40)'):
        changed.append(source_table + '.v2_effective_at')
    for column, ddl in (
        ('leased_by', 'VARCHAR(80)'),
        ('leased_at', 'VARCHAR(40)'),
        ('heartbeat_at', 'VARCHAR(40)'),
    ):
        if _add_column(conn, run_table, column, ddl):
            changed.append(run_table + '.' + column)
    if _add_column(conn, run_table, 'attempts', 'INTEGER', '0'):
        changed.append(run_table + '.attempts')
    # 历史记录沿用创建时规则：旧行统一标记为 v1（其原始单位即规范量纲）
    conn.execute(text(
        f"UPDATE {record_table} SET unit_version='v1' "
        "WHERE unit_version IS NULL OR unit_version=''"
    ))
    conn.execute(text(
        f"UPDATE {record_table} SET conversion_trace='{{}}' "
        "WHERE conversion_trace IS NULL"
    ))
    return changed


def run_migrations():
    from ..config.database import Base, Session
    # 先确保所有表（含新增表）存在
    Base.metadata.create_all(engine)
    applied = []
    with Session() as db:
        done = {v for (v,) in db.execute(text('SELECT version FROM schema_migrations')).fetchall()}
        for version, detail, fn in MIGRATIONS:
            if version in done:
                continue
            try:
                changed = fn(db.connection())
                db.add(SchemaMigration(version=version, applied_at=now(),
                                       detail=detail + ('；新增列：' + ','.join(changed) if changed else '')))
                db.commit()
                applied.append(version)
                logging.info('migration applied %s: %s', version, changed)
            except Exception:
                db.rollback()
                logging.exception('migration %s failed; will retry on next start', version)
                raise
    return applied
