"""主接口参数校验。手写而不用 FastAPI 自动校验：自动校验只会给 422，给不出可区分的错误码。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .errors import param_error

GRANULARITIES = ("day", "week", "month")
# 响应结构版本：加字段后递增。否则上线后、下一次回填前，旧结构的缓存还会按 24 小时 TTL 继续返回
RESPONSE_SCHEMA = "r2"
_IANA = available_timezones() | {"UTC"}


@dataclass(frozen=True)
class TrendParams:
    class_slug: str
    rule_version: str
    start: date
    end: date
    platforms: tuple[str, ...]
    tz: str
    granularity: str

    def cache_key(self, data_gen: str) -> str:
        # 平台已排序去重：app,website 与 website,app 命中同一个 key；
        # key 含规则版本和数据代号：改规则或回填后旧 key 自然作废，不用枚举删除；含结构版本：改响应结构同理
        return ":".join(["trend", RESPONSE_SCHEMA, self.class_slug, self.rule_version, data_gen, self.tz, self.granularity,
                         self.start.isoformat(), self.end.isoformat(), ",".join(self.platforms)])


def _date(name: str, v: str | None) -> date:
    if not v:
        raise param_error("INVALID_DATE", name, "required, format YYYY-MM-DD")
    try:
        if len(v) != 10:
            raise ValueError
        return date.fromisoformat(v)  # 2023-02-29 这类不存在的日期在这里被拒
    except ValueError:
        raise param_error("INVALID_DATE", name, f"'{v}' is not a valid YYYY-MM-DD date") from None


def parse_trend_params(*, class_slug: str, rule_version: str, start_date: str | None, end_date: str | None,
                       platforms: str | None, tz: str | None, granularity: str | None,
                       known_platforms: list[str], max_days: int) -> TrendParams:
    start, end = _date("start_date", start_date), _date("end_date", end_date)
    if end < start:
        raise param_error("RANGE_INVERTED", "end_date", "must not be earlier than start_date")
    days = (end - start).days + 1
    if days > max_days:
        raise param_error("RANGE_TOO_LARGE", "end_date", f"range is {days} days; maximum is {max_days}")

    if platforms:
        req = sorted({p.strip().lower() for p in platforms.split(",") if p.strip()})
        bad = [p for p in req if p not in known_platforms]
        if bad:
            raise param_error("UNKNOWN_PLATFORM", "platforms",
                              f"unknown: {','.join(bad)}; known: {','.join(sorted(known_platforms))}")
    else:
        req = sorted(known_platforms)

    tz = tz or "UTC"
    # 只收 IANA 名，不收 +08:00：固定偏移处理不了夏令时
    if tz not in _IANA:
        raise param_error("INVALID_TZ", "tz", f"'{tz}' is not an IANA time zone name, e.g. Asia/Shanghai")
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise param_error("INVALID_TZ", "tz", f"'{tz}' is not available") from None

    granularity = granularity or "day"
    if granularity not in GRANULARITIES:
        raise param_error("INVALID_GRANULARITY", "granularity", f"must be one of {', '.join(GRANULARITIES)}")

    return TrendParams(class_slug, rule_version, start, end, tuple(req), tz, granularity)
