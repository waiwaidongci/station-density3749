"""上报行接纳管道：按生效时间判定单位版本 → 转换规范量纲 → 校验 → 落库/隔离。

设计要点：
- 未知版本、与来源生效版本冲突（含迟到旧版本）、字段校验失败的行进入隔离区，
  原始报文完整保留，修正后可重放（replay）。
- 自然键 (source_id, reference) 幂等：重复接纳不产生第二条；重放也不重复。
- 已冻结研究在创建时固化快照，任何迟到行只进来源表/隔离区，绝不改写历史输入。
"""
import secrets
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select
from ..models.record import Record
from ..models.quarantine import Quarantine
from ..config.settings import CONFIG
from ..utils.serialization import now
from .audit import log
from .domain_validation import validate_record
from . import unit_versions as uv

# 隔离原因码
REASON_UNKNOWN_VERSION = 'unknown_unit_version'
REASON_VERSION_CONFLICT = 'unit_version_conflict'
REASON_LATE_OLD_VERSION = 'late_old_unit_version'
REASON_INVALID_VALUE = 'invalid_record_value'
REASON_DUPLICATE_CONFLICT = 'duplicate_payload_conflict'


def _quarantine(db, source_id, item, reason_code, reason_detail, batch_id, replay_of=None):
    q = Quarantine(
        source_id=source_id,
        reference=item.get('reference', '')[:120],
        raw_payload=item.get('values', {}),
        claimed_version=item.get('unit_version'),
        observed_at=item.get('observed_at'),
        reason_code=reason_code,
        reason_detail=reason_detail,
        state='open',
        replay_of=replay_of,
        batch_id=batch_id,
        created_at=now(),
        updated_at=now(),
    )
    db.add(q)
    db.flush()
    return q


def _ingest_one(db, source, item, batch_id, replay_of=None):
    """处理单行，返回 ('accepted'|'quarantined', Record|Quarantine)。"""
    source_id = source.id
    reference = item.get('reference')
    claimed = item.get('unit_version') or uv.DEFAULT_VERSION
    observed_at = item.get('observed_at')

    # 1) 未知版本 → 隔离
    if claimed not in uv.UNIT_VERSIONS:
        q = _quarantine(db, source_id, item, REASON_UNKNOWN_VERSION,
                        f"未知字段单位版本 {claimed!r}，已知版本：{', '.join(uv.KNOWN_VERSIONS)}",
                        batch_id, replay_of)
        return 'quarantined', q

    # 观测时间必须是可解析的 UTC ISO8601 字符串，否则无法按生效时间接纳
    if observed_at is not None:
        try:
            datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            q = _quarantine(db, source_id, item, REASON_INVALID_VALUE,
                            f'观测时间 observed_at 不是合法 ISO8601 时间：{observed_at}',
                            batch_id, replay_of)
            return 'quarantined', q

    expected = uv.effective_version(source, observed_at)
    # 2) 与生效时间应接纳的版本冲突 → 隔离（含换代后仍上报 v1 的迟到旧版本）
    if claimed != expected:
        if uv.is_late_old_version(source, observed_at, claimed):
            code, detail = REASON_LATE_OLD_VERSION, (
                f"换代已于 {getattr(source, 'v2_effective_at', None) or uv.CUTOVER_AT} 生效，"
                f"迟到的 v1（℃/μg/m³）上报不得混入 v2 数据，请核对后按 v2 重报")
        else:
            code, detail = REASON_VERSION_CONFLICT, (
                f"观测时间 {observed_at or '（缺失）'} 的生效版本为 {expected}，"
                f"实际上报 {claimed}，单位版本与生效时间冲突")
        q = _quarantine(db, source_id, item, code, detail, batch_id, replay_of)
        return 'quarantined', q

    # 3) 数值校验（先按上报单位做原始可解析性检查，再转换）
    raw_values = item.get('values', {})
    try:
        # 借用规范校验前先确认字段齐全且可转 float
        precheck = {}
        for key, label, kind in CONFIG['fields']:
            v = raw_values.get(key)
            if kind == 'number':
                if isinstance(v, bool):
                    raise ValueError(label + '必须为数值')
                precheck[key] = float(v)
            else:
                precheck[key] = v
        canonical, trace = uv.convert_values(precheck, claimed)
        canonical = validate_record(canonical)
    except (TypeError, ValueError, KeyError) as e:
        q = _quarantine(db, source_id, item, REASON_INVALID_VALUE, str(e)[:600], batch_id, replay_of)
        return 'quarantined', q

    # 4) 自然键幂等：同键已存在则核对内容，冲突隔离，否则跳过
    existing = db.scalar(select(Record).where(Record.source_id == source_id, Record.reference == reference))
    if existing:
        if existing.unit_version != claimed or existing.values != canonical:
            q = _quarantine(db, source_id, item, REASON_DUPLICATE_CONFLICT,
                            f"样本编号 {reference} 已存在且内容/版本不一致，追加上报被隔离，"
                            "已冻结研究与既有记录不被改写", batch_id, replay_of)
            return 'quarantined', q
        return 'accepted', existing

    record = Record(
        source_id=source_id,
        reference=reference,
        values=canonical,
        unit_version=claimed,
        observed_at=observed_at,
        conversion_trace=trace,
    )
    db.add(record)
    db.flush()
    return 'accepted', record


def ingest_batch(db, user, source, items, batch_id=None, audit=True):
    """批量接纳。返回接纳/隔离统计与隔离行明细；整批在同一事务，失败整体回滚。"""
    batch_id = batch_id or 'batch-' + secrets.token_hex(8)
    accepted, quarantined = 0, 0
    q_rows = []
    refs = set()
    for item in items:
        ref = item.get('reference')
        if not ref or not isinstance(ref, str):
            raise HTTPException(422, '样本编号 reference 为必填字符串')
        if ref in refs:
            raise HTTPException(422, '批内样本编号重复：' + ref)
        refs.add(ref)
        outcome, obj = _ingest_one(db, source, item, batch_id)
        if outcome == 'accepted':
            accepted += 1
        else:
            quarantined += 1
            q_rows.append({'id': obj.id, 'reference': obj.reference,
                           'reason_code': obj.reason_code, 'reason_detail': obj.reason_detail})
    if audit:
        log(db, user, 'records.ingested', source.id,
            f'batch={batch_id} accepted={accepted} quarantined={quarantined}')
        if quarantined:
            log(db, user, 'records.quarantined', source.id,
                f'batch={batch_id} 隔离 {quarantined} 行，待修正重放')
    db.commit()
    return {'batch_id': batch_id, 'accepted': accepted, 'quarantined': quarantined,
            'quarantine_rows': q_rows}


def list_quarantine(db, source_id=None, state=None):
    stmt = select(Quarantine).order_by(Quarantine.id.desc())
    if source_id is not None:
        stmt = stmt.where(Quarantine.source_id == source_id)
    if state:
        stmt = stmt.where(Quarantine.state == state)
    return [_q_row(q) for q in db.scalars(stmt.limit(500))]


def _q_row(q):
    return {c.name: getattr(q, c.name) for c in q.__table__.columns}


def replay_quarantine(db, user, quarantine_id, override=None):
    """重放隔离行：可携带修正后的报文/版本/观测时间；成功则关闭隔离并登记去向。"""
    q = db.get(Quarantine, quarantine_id)
    if not q:
        raise HTTPException(404, '未找到隔离记录')
    if q.state == 'replayed':
        raise HTTPException(409, '该隔离行已重放，不能重复处理')
    from ..models.source import Source
    source = db.get(Source, q.source_id)
    if not source:
        raise HTTPException(404, '隔离行所属来源已不存在')

    item = {
        'reference': q.reference,
        'values': (override or {}).get('values', q.raw_payload),
        'unit_version': (override or {}).get('unit_version', q.claimed_version),
        'observed_at': (override or {}).get('observed_at', q.observed_at),
    }
    batch_id = 'replay-' + secrets.token_hex(8)
    outcome, obj = _ingest_one(db, source, item, batch_id, replay_of=q.id)
    q.state = 'replayed' if outcome == 'accepted' else 'open'
    q.updated_at = now()
    if outcome == 'accepted':
        q.resolved_record_id = obj.id
        log(db, user, 'quarantine.replayed', q.id,
            f'隔离行 #{q.id} 重放成功 → 记录 #{obj.id}（版本 {obj.unit_version}）')
    else:
        # 再次隔离：关闭旧行、保留新行与重放链，避免同一条长期挂起无法追踪
        q.state = 'superseded'
        log(db, user, 'quarantine.replay_failed', q.id,
            f'隔离行 #{q.id} 重放仍被隔离 → 新隔离行 #{obj.id}：{obj.reason_code}')
    db.commit()
    return {'outcome': outcome, 'quarantine': _q_row(q),
            'next': _q_row(obj) if outcome != 'accepted' else {'record_id': q.resolved_record_id}}


def discard_quarantine(db, user, quarantine_id):
    """人工判定无效并关闭隔离行（保留原始报文与审计痕迹）。"""
    q = db.get(Quarantine, quarantine_id)
    if not q:
        raise HTTPException(404, '未找到隔离记录')
    if q.state != 'open':
        raise HTTPException(409, '仅待处理（open）隔离行可以关闭')
    q.state = 'discarded'
    q.updated_at = now()
    log(db, user, 'quarantine.discarded', q.id, f'隔离行 #{q.id} 人工关闭')
    db.commit()
    return _q_row(q)
