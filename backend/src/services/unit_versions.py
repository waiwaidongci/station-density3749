"""字段单位版本注册表与量纲转换。

传感器换代后，部分微站以 v2 单位上报（温度 ℉、颗粒 mg/m³），旧记录为
v1（℃、μg/m³）。系统内部只保存统一规范量纲（canonical：℃、%、μg/m³），
每行记录同时保留来源版本与转换痕迹，保证页面与导出同量纲且可追查。
"""
from ..config.settings import CONFIG

CANONICAL_UNITS = CONFIG['canonical_units']
UNIT_VERSIONS = CONFIG['unit_versions']
DEFAULT_VERSION = CONFIG['default_unit_version']
CUTOVER_AT = CONFIG['unit_cutover_at']
LATE_GRACE_HOURS = CONFIG.get('late_grace_hours', 24)
KNOWN_VERSIONS = tuple(UNIT_VERSIONS.keys())


def version_label(version):
    info = UNIT_VERSIONS.get(version)
    return info['label'] if info else str(version)


def source_units(version):
    """返回该上报版本每个字段的原始单位；未知版本返回 None。"""
    info = UNIT_VERSIONS.get(version)
    return dict(info['units']) if info else None


def f_to_c(value):
    return (value - 32.0) * 5.0 / 9.0


def c_to_f(value):
    return value * 9.0 / 5.0 + 32.0


def convert_field(field, value, from_version):
    """把单个字段值从上报版本单位转换为规范量纲。

    返回 (规范值, 转换说明)；无需转换时说明为 'identity'。
    未知版本抛 KeyError，由调用方隔离该行。
    """
    units = UNIT_VERSIONS[from_version]['units']
    canonical = CANONICAL_UNITS[field]
    if units[field] == canonical:
        return value, 'identity'
    if field == 'temperature' and units[field] == '℉' and canonical == '℃':
        return f_to_c(value), 'F_to_C:(F-32)*5/9'
    if field == 'particles' and units[field] == 'mg/m³' and canonical == 'μg/m³':
        return value * 1000.0, 'mg/m3_to_ug/m3:*1000'
    raise KeyError(f'未配置 {field} 从 {units[field]} 到 {canonical} 的转换规则')


def convert_values(values, version):
    """整行业务字段转换为规范量纲，返回 (规范值, 每字段转换说明)。"""
    if version not in UNIT_VERSIONS:
        raise KeyError('未知字段单位版本：' + str(version))
    out, trace = {}, {}
    for key, _label, kind in CONFIG['fields']:
        if kind != 'number':
            out[key] = values[key]
            trace[key] = 'identity'
            continue
        out[key], trace[key] = convert_field(key, values[key], version)
    return out, trace


def effective_version(source, observed_at):
    """按生效时间判定来源在 observed_at 时刻应接纳的单位版本。

    来源可通过 v2_effective_at 单独配置换代时刻；未配置则使用全局
    unit_cutover_at。observed_at 早于生效时间接纳 v1，之后接纳 v2。
    """
    cutoff = getattr(source, 'v2_effective_at', None) or CUTOVER_AT
    if observed_at and observed_at >= cutoff:
        return 'v2'
    return 'v1'


def is_late_old_version(source, observed_at, version):
    """迟到的旧版本：换代生效后（含宽限期）仍按 v1 上报。"""
    if version != 'v1':
        return False
    cutoff = getattr(source, 'v2_effective_at', None) or CUTOVER_AT
    if not observed_at or observed_at < cutoff:
        return False
    return True
