"""查询规划器：把一个本地日期闭区间拆成三段，每段用最便宜的表。

    A class_daily  tz 在预设列表，且该本地日完整落在 sealed_until 之前
    B hourly       tz 不在预设列表但当天是整点偏移，且已封口
    C fact         当天未封口；或非整点偏移且不在预设列表

已封口的读物化结果、未封口的读明细，是 Druid / Timescale 实时聚合的通用思路；
这里多做的一步是把「时区」也纳入分段条件。规划是纯函数，执行在 executor 里。

null / partial 按平台判：每个平台有自己的截止时间（从该平台最新事件推断），
晚于它的日子对这个平台是 null，包含它的那天对这个平台不完整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SEGMENT_TABLE = {"class_daily": "agg_class_daily", "hourly": "agg_product_hourly", "fact": "fact_sales"}


def local_day_utc(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """本地日 d 对应的 UTC 半开区间。夏令时切换日自然是 23 或 25 小时。"""
    start = datetime.combine(d, time(0), tz).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(0), tz).astimezone(UTC)
    return start, end


def is_whole_hour_day(d: date, tz: ZoneInfo) -> bool:
    for t in (datetime.combine(d, time(0), tz), datetime.combine(d, time(23, 59), tz)):
        if t.utcoffset().total_seconds() % 3600:
            return False
    return True


@dataclass
class Segment:
    segment: str  # class_daily | hourly | fact
    dates: list[date]

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    def utc_range(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        return local_day_utc(self.first, tz)[0], local_day_utc(self.last, tz)[1]

    def describe(self) -> dict:
        return {"segment": self.segment, "table": SEGMENT_TABLE[self.segment],
                "from": self.first.isoformat(), "to": self.last.isoformat()}


@dataclass
class Plan:
    days: list[date]
    segments: list[Segment] = field(default_factory=list)
    null_dates: set[date] = field(default_factory=set)      # 晚于所有选中平台的截止：返回 null，不查库
    partial_dates: set[date] = field(default_factory=set)   # 某个平台不完整或为 null、但不是全部 null 的日
    fully_sealed: bool = True                               # 决定缓存 TTL 档位
    null_by_platform: dict[str, set[date]] = field(default_factory=dict)  # 晚于该平台截止的日


def plan_query(start: date, end: date, tz_name: str, *, sealed_until: datetime | None,
               data_through: datetime | None, preset_timezones: tuple[str, ...],
               force: str | None = None, platform_through: dict[str, datetime | None] | None = None) -> Plan:
    """force 仅供对拍测试：强制所有可查日走同一段，验证三条路径结果一致。

    platform_through 给出选中平台各自的截止时间；给了就以其中最晚的一个作为 data_through
    （查库范围 = 至少有一个平台有数据的日），再逐平台标 null 与 partial。
    """
    tz = ZoneInfo(tz_name)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    plan = Plan(days=days)
    if platform_through:
        known = [t for t in platform_through.values() if t is not None]
        data_through = max(known) if known else None
    through_local = data_through.astimezone(tz).date() if data_through else None

    kinds: list[tuple[date, str]] = []
    for d in days:
        _, d_end = local_day_utc(d, tz)
        if through_local is None or d > through_local:
            plan.null_dates.add(d)
            continue
        if data_through and d_end > data_through:
            plan.partial_dates.add(d)
        sealed = sealed_until is not None and d_end <= sealed_until
        if not sealed:
            plan.fully_sealed = False
        if force:
            kind = force
        elif sealed and tz_name in preset_timezones:
            kind = "class_daily"
        elif sealed and is_whole_hour_day(d, tz):
            kind = "hourly"
        else:
            kind = "fact"
        kinds.append((d, kind))

    for d, kind in kinds:  # 连续同类的日合并成一段
        if plan.segments and plan.segments[-1].segment == kind and plan.segments[-1].last + timedelta(1) == d:
            plan.segments[-1].dates.append(d)
        else:
            plan.segments.append(Segment(kind, [d]))
    for src, through in (platform_through or {}).items():
        local = through.astimezone(tz).date() if through else None
        nulls = {d for d in days if local is None or d > local}
        plan.null_by_platform[src] = nulls
        if local in days:
            plan.partial_dates.add(local)  # 该平台截止时刻所在的日，对它不完整
        # 部分平台 null、部分有数：total 只是有数平台之和，这一天同样不完整
        plan.partial_dates |= nulls - plan.null_dates
    if plan.null_dates or any(plan.null_by_platform.values()):
        plan.fully_sealed = False  # 还没数据的日子随时可能来数据，不能按「已封口」长缓存
    return plan
