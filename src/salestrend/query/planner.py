"""查询规划器：把一个本地日期闭区间拆成若干段，每段用最便宜的表。

    A class_daily  tz 在预设列表，且该本地日完整落在 sealed_until 之前                     → agg_class_daily
    B class_15m    其余已封口的部分（任意时区，含未封口那天里 sealed_until 之前的部分）     → agg_class_15m
    C fact         sealed_until 之后的尾巴；以及日界不落在 15 分钟格上的零头（现行时区没有）  → fact_sales

为什么是 15 分钟：现行全部时区的偏移都是 15 分钟的整数倍（+05:45 Kathmandu、+08:45 Eucla、+12:45 Chatham），
15 分钟桶能切准任何时区的日界，所以非整点、又不在预设里的时区也不用再扫明细。品类层每个规则版本一年
至多 3 平台 × 35,136 格 ≈ 10.5 万行（P5 实测 7 万），一年区间 8 并发 P99 与小时粒度同一量级（15 vs 14 ms）。

切分按 UTC 半开区间算，且在一天之内也切：未封口那天只有 [sealed_until, 日终) 读明细，前半天读品类表。
v1.2.0 是「当天没封口，整天读明细」，实时请求的明细段因此是一整天；现在是封口线之后的尾巴（≤ 15 分钟 +
重算间隔），sealed_until 由重算按 15 分钟对齐。meta.plan 的 utc_from / utc_to 回显每段的精确 UTC 区间。

product_15m 只供 force 对拍：商品层是重算中间层，规划器自己不会选它（稀疏数据下它的行数接近明细，
品类商品分散、按 product_id 打头的排序键跳不过 granule，一年区间等于全表扫描）。

已封口的读物化结果、未封口的读明细，是 Druid / Timescale 实时聚合的通用思路；
这里多做的一步是把「时区」也纳入分段条件。规划是纯函数，执行在 executor 里。

null / partial 按平台判：每个平台有自己的截止时间（从该平台最新事件推断），
晚于它的日子对这个平台是 null，包含它的那天对这个平台不完整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SEGMENT_TABLE = {"class_daily": "agg_class_daily", "class_15m": "agg_class_15m", "fact": "fact_sales",
                 "product_15m": "agg_product_15m"}
BUCKET = timedelta(minutes=15)


def local_day_utc(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """本地日 d 对应的 UTC 半开区间。夏令时切换日自然是 23 或 25 小时。"""
    start = datetime.combine(d, time(0), tz).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(0), tz).astimezone(UTC)
    return start, end


def on_grid(t: datetime) -> bool:
    """是否落在 15 分钟格上。历史上的地方平时（如 1972 年前的 Africa/Monrovia −0:44:30）不在格上，退回明细。"""
    return t.minute % 15 == 0 and t.second == 0 and t.microsecond == 0


def floor_bucket(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Segment:
    segment: str  # class_daily | class_15m | fact | product_15m（仅对拍）
    dates: list[date]
    utc_from: datetime
    utc_to: datetime  # 半开区间；一天之内切分时，同一个本地日可以分属两段

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    def describe(self) -> dict:
        return {"segment": self.segment, "table": SEGMENT_TABLE[self.segment],
                "from": self.first.isoformat(), "to": self.last.isoformat(),
                "utc_from": _iso(self.utc_from), "utc_to": _iso(self.utc_to)}


@dataclass
class Plan:
    days: list[date]
    segments: list[Segment] = field(default_factory=list)
    null_dates: set[date] = field(default_factory=set)      # 晚于所有选中平台的截止：返回 null，不查库
    partial_dates: set[date] = field(default_factory=set)   # 某个平台不完整或为 null、但不是全部 null 的日
    fully_sealed: bool = True                               # 决定缓存 TTL 档位
    null_by_platform: dict[str, set[date]] = field(default_factory=dict)  # 晚于该平台截止的日


def _pieces(s: datetime, e: datetime, *, sealed_until: datetime | None, preset: bool,
            force: str | None) -> list[tuple[str, datetime, datetime]]:
    """一个本地日拆成（段, UTC 起, UTC 止）若干块。"""
    if force:
        return [(force, s, e)]
    if sealed_until is not None and e <= sealed_until:
        if preset:
            return [("class_daily", s, e)]
        return [("class_15m", s, e)] if on_grid(s) and on_grid(e) else [("fact", s, e)]
    # 未封口：封口线之前读品类表，之后的尾巴读明细。封口线由重算按 15 分钟对齐，这里仍做一次校验
    if sealed_until is not None and s < sealed_until and on_grid(s) and on_grid(sealed_until):
        return [("class_15m", s, sealed_until), ("fact", sealed_until, e)]
    return [("fact", s, e)]


def plan_query(start: date, end: date, tz_name: str, *, sealed_until: datetime | None,
               data_through: datetime | None, preset_timezones: tuple[str, ...],
               force: str | None = None, platform_through: dict[str, datetime | None] | None = None) -> Plan:
    """force 仅供对拍测试：强制所有可查日整天走同一段（含 product_15m），验证各条路径结果一致。

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

    preset = tz_name in preset_timezones
    for d in days:
        d_start, d_end = local_day_utc(d, tz)
        if through_local is None or d > through_local:
            plan.null_dates.add(d)
            continue
        if data_through and d_end > data_through:
            plan.partial_dates.add(d)
        if not (sealed_until is not None and d_end <= sealed_until):
            plan.fully_sealed = False
        for kind, s, e in _pieces(d_start, d_end, sealed_until=sealed_until, preset=preset, force=force):
            last = plan.segments[-1] if plan.segments else None
            if last and last.segment == kind and last.utc_to == s:  # 首尾相接的同类块合并成一次查询
                last.utc_to = e
                if last.dates[-1] != d:
                    last.dates.append(d)
            else:
                plan.segments.append(Segment(kind, [d], s, e))
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
