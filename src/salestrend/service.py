"""趋势查询服务：整响应缓存 → singleflight → 规划 → 月块缓存 / 执行 → 合并补零 → 组装响应。存储故障时降级到旧缓存。

两层缓存（design.md §7）：第一层按整个响应，热点请求一次 GET 返回；第一层未命中时走第二层月块，
已封口的日从月块拼，缺的块与未封口的日才查 ClickHouse。两层拼出来的逐日值交给同一个 assemble，口径同源。
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .api.errors import Problem
from .api.params import TrendParams
from .cache.blocks import BlockCache, block_key, encode, last_sealed_day, month_end, month_of, usable
from .cache.resilience import CircuitBreaker, KeyedFlight, ResponseCache, SingleFlight
from .query.planner import Plan, Segment, plan_query
from .settings import Settings

log = logging.getLogger(__name__)

TTL_SEALED_S = 24 * 3600   # 区间全部已封口：历史数据不会变
TTL_OPEN_S = 60            # 含未封口日或未来日


def _iso_z(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _bucket(d: date, granularity: str) -> date:
    if granularity == "week":
        return d - timedelta(days=d.weekday())  # ISO 周，周一为起点
    if granularity == "month":
        return d.replace(day=1)
    return d


def _bucket_len(b: date, granularity: str) -> int:
    return 7 if granularity == "week" else calendar.monthrange(b.year, b.month)[1]


_UTC_NAMES = {"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Etc/Zulu", "Zulu", "Universal", "Etc/Universal",
              "GMT", "Etc/GMT", "Etc/GMT0", "Etc/GMT+0", "Etc/GMT-0", "GMT0", "GMT+0", "GMT-0", "Greenwich"}


def _same_tz(a: str, b: str) -> bool:
    return a == b or (a in _UTC_NAMES and b in _UTC_NAMES)


def source_tz_meta(s: Settings, platforms: tuple[str, ...], tz: str) -> tuple[bool, list[dict]]:
    """源时区没确认时，按别的时区出报表会把「假设错了」放大成整体偏移：不拒绝，但要说出来。

    例：源时间其实是北京时间，却按 UTC 入库，再请求 tz=Asia/Shanghai，每条事件又被往后推 8 小时，
    样例 2 月合计从 445 变成 415。请求 tz 与源时区相同时，换算是恒等的，假设错不错都不影响切日。
    """
    unconfirmed = [src for src in platforms if not s.source_tz_confirmed.get(src, False)]
    shifted = [src for src in unconfirmed if not _same_tz(s.platforms.get(src, "UTC"), tz)]
    warnings = []
    if shifted:
        warnings.append({
            "code": "SOURCE_TZ_UNCONFIRMED",
            "message": (f"source time zone of {', '.join(shifted)} is assumed, not confirmed with the data "
                        f"provider; converting to {tz} may shift every event by a constant offset"),
            "platforms": shifted,
            "source_tz": {src: s.platforms.get(src, "UTC") for src in shifted},
        })
    return bool(unconfirmed), warnings


class TrendService:
    def __init__(self, s: Settings, store, cache: ResponseCache | None, store_factory=None,
                 blocks: BlockCache | None = None):
        self.s, self.store, self.cache, self._factory, self.blocks = s, store, cache, store_factory, blocks
        self.flight = SingleFlight()
        self.block_flight = KeyedFlight()
        self.breaker = CircuitBreaker()
        self._state: tuple[float, dict] = (0.0, {})

    def get_store(self):
        if self.store is None:
            self.store = self._factory()
        return self.store

    async def _call_store(self, method: str, *args):
        if self.breaker.is_open:
            raise Problem("UPSTREAM_UNAVAILABLE", "storage circuit open, retry later")
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(lambda: getattr(self.get_store(), method)(*args)), self.s.query_timeout_s + 0.5)
        except TimeoutError:
            self.breaker.failure()
            raise Problem("QUERY_TIMEOUT", f"query exceeded {self.s.query_timeout_s}s") from None
        except Problem:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("storage call %s failed: %s: %s", method, type(e).__name__, e)  # 不留日志 = 静默的 503
            self.breaker.failure()
            msg = str(e).lower()
            code = "QUERY_TIMEOUT" if "timeout" in msg or "timed out" in msg else "UPSTREAM_UNAVAILABLE"
            raise Problem(code, f"storage error: {type(e).__name__}") from None
        self.breaker.success()
        return result

    async def pipeline_state(self) -> dict:
        ts, st = self._state
        if time.monotonic() - ts > 5 or not st:  # 水位进程内缓存 5 秒
            st = await self._call_store("state")
            self._state = (time.monotonic(), st)
        return st

    async def trend(self, p: TrendParams) -> tuple[dict, int]:
        """返回（响应体，缓存 TTL 秒）。"""
        # 读不到 data_gen（Redis 故障）：两层都不读不写，直接查库
        data_gen = await self.cache.data_gen() if self.cache else None
        key = p.cache_key(data_gen) if data_gen is not None else None
        if key and (hit := await self.cache.get(key)):
            hit["meta"]["cache"] = "hit"
            self._refresh_tz_meta(hit, p)
            return hit, TTL_SEALED_S if hit["meta"].pop("_sealed", False) else TTL_OPEN_S

        flight_key = key or p.cache_key("nocache")
        try:
            body, ttl = await self.flight.do(flight_key, lambda: self._compute(p, key, data_gen))
        except Problem as e:
            if e.code in ("UPSTREAM_UNAVAILABLE", "QUERY_TIMEOUT") and key and (
                stale := await self.cache.get(key, stale=True)
            ):
                stale["meta"].pop("_sealed", None)
                stale["meta"].update(cache="stale", stale=True)
                self._refresh_tz_meta(stale, p)
                return stale, 0
            raise
        body = {"data": body["data"], "meta": {**body["meta"]}}
        body["meta"].pop("_sealed", None)
        return body, ttl

    def _refresh_tz_meta(self, body: dict, p: TrendParams) -> None:
        # 时区警告来自配置而不是数据：只改 confirmed 不会重放、不会换 data_gen，所以缓存命中时按当前配置重算
        assumed, warnings = source_tz_meta(self.s, p.platforms, p.tz)
        body["meta"].update(source_tz_assumed=assumed, warnings=warnings)

    async def _compute(self, p: TrendParams, key: str | None, data_gen: str | None) -> tuple[dict, int]:
        st = await self.pipeline_state()
        # 各平台截止：rollup 写的 data_through:{src}；旧水位里没有时退回全局截止
        through_by = {src: st.get(f"data_through:{src}", st.get("data_through")) for src in p.platforms}
        # 整个区间的计划总要算：null / partial / 桶都以它为准，月块只替换「值从哪来」
        plan = plan_query(p.start, p.end, p.tz, sealed_until=st.get("sealed_until"),
                          data_through=st.get("data_through"), preset_timezones=self.s.preset_timezones,
                          platform_through=through_by)
        if self.blocks is not None and data_gen is not None:
            segments, cells, tag = await self._via_blocks(p, plan, st, through_by, data_gen)
        else:
            segments, tag = plan.segments, "miss"
            cells = await self._call_store("run_plan", plan, p.class_slug, p.rule_version, p.tz,
                                           list(p.platforms))
        assumed, warnings = source_tz_meta(self.s, p.platforms, p.tz)
        body = self.assemble(p, plan, cells, st.get("data_through"), through_by=through_by,
                             source_tz_assumed=assumed, warnings=warnings, segments=segments)
        ttl = TTL_SEALED_S if plan.fully_sealed else TTL_OPEN_S
        if key:
            await self.cache.set(key, {**body, "meta": {**body["meta"], "_sealed": plan.fully_sealed}}, ttl)
        body["meta"]["cache"] = tag
        return body, ttl

    async def _via_blocks(self, p: TrendParams, plan: Plan, st: dict, through_by: dict,
                          data_gen: str) -> tuple[list[Segment], dict, str]:
        """已封口的日从月块取，缺块合并成一次查询回填；未封口的日按现有规划器现查。

        返回（真正查了 ClickHouse 的段, 逐日值, meta.cache）。meta.cache：没用上任何块 = miss；
        全部来自块、没查库 = block；块 + 查库 = block_partial。
        """
        tz = ZoneInfo(p.tz)
        last = last_sealed_day(tz, st.get("sealed_until"))
        # 每个（月, 平台）要从块里取的日：已封口、且该平台这一天不是 null（null 由 assemble 按计划补，不用取）
        need: dict[tuple[date, str], list[date]] = defaultdict(list)
        for d in plan.days:
            if last is None or d > last:
                break
            if d in plan.null_dates:
                continue
            for src in p.platforms:
                if d not in plan.null_by_platform.get(src, ()):
                    need[(month_of(d), src)].append(d)
        keys = {mk: block_key(p.class_slug, p.rule_version, data_gen, p.tz, mk[1], mk[0]) for mk in need}
        got = dict(zip(keys, await self.blocks.mget(list(keys.values())), strict=True))
        blocks = {mk: b for mk, b in got.items() if usable(b, need[mk], through_by[mk[1]])}
        segments: list[Segment] = []
        fetched: set[tuple[date, str]] = set()

        if missing := {keys[mk]: mk for mk in need if mk not in blocks}:
            async def fetch(ks: list[str]) -> dict[str, dict]:
                segs, filled = await self._fill_blocks(p, [missing[k] for k in ks], st, through_by, last, data_gen)
                segments.extend(segs)
                fetched.update(filled)
                return {keys[mk]: b for mk, b in filled.items()}

            res = await self.block_flight.do(list(missing), fetch)
            left = []
            for k, mk in missing.items():
                if usable(res.get(k), need[mk], through_by[mk[1]]):
                    blocks[mk] = res[k]
                else:
                    left.append(mk)  # 等来的块是别人按另一份水位填的（并发期间水位刷新了）：自己补，不再合并
            if left:
                segs, filled = await self._fill_blocks(p, left, st, through_by, last, data_gen)
                segments.extend(segs)
                fetched.update(filled)
                blocks.update(filled)

        cells: dict[tuple[date, str], int] = {}
        for mk, days in need.items():
            v = blocks[mk]["v"]
            for d in days:
                cells[(d, mk[1])] = v[d.day - 1]
        # 未封口的日（当天、封口线之后）每次现查，走现有规划器，只查这些日
        live_from = p.start if last is None else max(p.start, last + timedelta(days=1))
        if live_from <= p.end:
            live = plan_query(live_from, p.end, p.tz, sealed_until=st.get("sealed_until"),
                              data_through=st.get("data_through"), preset_timezones=self.s.preset_timezones,
                              platform_through=through_by)
            if live.segments:
                cells.update(await self._call_store("run_plan", live, p.class_slug, p.rule_version, p.tz,
                                                    list(p.platforms)))
                segments.extend(live.segments)
        reused = any(mk not in fetched for mk in need)
        return segments, cells, ("block_partial" if segments else "block") if reused else "miss"

    async def _fill_blocks(self, p: TrendParams, mks: list[tuple[date, str]], st: dict, through_by: dict,
                           last: date, data_gen: str) -> tuple[list[Segment], dict[tuple[date, str], dict]]:
        """缺的（月, 平台）合并成一次规划：从最早缺块的月初到最晚缺块的月末（截到最后一个封口日），
        平台取缺块平台的并集。中间夹着的已有块会被顺带重查一遍：多读一个月的日表行，换少一次往返，划算。
        规划器照常分段（预设时区一段日表，其余时区一段 15 分钟表），回来以后整月写回，不只写请求覆盖的那几天。
        """
        months = sorted({m for m, _ in mks})
        srcs = sorted({src for _, src in mks})
        fp = plan_query(months[0], min(month_end(months[-1]), last), p.tz, sealed_until=st.get("sealed_until"),
                        data_through=st.get("data_through"), preset_timezones=self.s.preset_timezones,
                        platform_through={src: through_by[src] for src in srcs})
        cells = (await self._call_store("run_plan", fp, p.class_slug, p.rule_version, p.tz, srcs)
                 if fp.segments else {})
        tz = ZoneInfo(p.tz)
        out = {}
        for m, src in mks:
            upto = min(month_end(m), last)
            days = [m + timedelta(days=i) for i in range((upto - m).days + 1)]
            nulls = fp.null_by_platform.get(src, set())
            vals = [None if d in fp.null_dates or d in nulls else cells.get((d, src), 0) for d in days]
            out[(m, src)] = encode(vals, through_by[src], tz, upto)
        await self.blocks.mset({block_key(p.class_slug, p.rule_version, data_gen, p.tz, src, m): b
                                for (m, src), b in out.items()})
        return fp.segments, out

    @staticmethod
    def assemble(p: TrendParams, plan, cells: dict, data_through: datetime | None, *,
                 through_by: dict[str, datetime | None] | None = None, source_tz_assumed: bool = False,
                 warnings: list[dict] | None = None, segments: list[Segment] | None = None) -> dict:
        """segments：真正查了 ClickHouse 的段，写进 meta.plan；不给就是 plan.segments（不经月块时两者相同）。"""
        # 按日补零：没卖出去 = 0；晚于该平台的数据截止 = null。前端画折线时 null 断开、0 落地，含义不同
        def is_null(d: date, src: str) -> bool:
            return d in plan.null_dates or d in plan.null_by_platform.get(src, ())

        daily: dict[str, dict[date, int | None]] = {
            src: {d: (None if is_null(d, src) else cells.get((d, src), 0)) for d in plan.days}
            for src in p.platforms
        }
        buckets = sorted({_bucket(d, p.granularity) for d in plan.days})
        labels = [max(b, p.start) for b in buckets]  # 首个周/月桶从 start_date 起算

        def roll(vals: dict[date, int | None]) -> list[int | None]:
            # 一趟累加：原来每个桶各扫一遍全部日子，一年 day 粒度是 366 × 366 × 平台数次比较，
            # 单请求 13 ms 纯 CPU，在 50–100 ms 的端到端预算里占得太多。桶内全为 null 时仍返回 null
            acc: dict[date, int] = {}
            for d, v in vals.items():
                if v is not None:
                    b = _bucket(d, p.granularity)
                    acc[b] = acc.get(b, 0) + v
            return [acc.get(b) for b in buckets]

        series = [{"platform": src, "values": roll(daily[src])} for src in p.platforms]
        total = [None if all(s["values"][i] is None for s in series)
                 else sum(s["values"][i] or 0 for s in series) for i in range(len(buckets))]
        sums = {s["platform"]: sum(v or 0 for v in s["values"]) for s in series}
        sums["all"] = sum(sums.values())
        data = {"dates": [d.isoformat() for d in labels], "series": series, "total": total}
        if p.granularity != "day":
            # 不完整周期给日均：3 月只到 13 号，按月合计像「崩了」，日均才可比。
            # days_covered = 桶内落在查询区间、且至少一个选中平台已有数据的日（截止当天算一整天）
            covered = [sum(1 for d in plan.days if _bucket(d, p.granularity) == b and d not in plan.null_dates)
                       for b in buckets]
            sizes = [_bucket_len(b, p.granularity) for b in buckets]
            data["days_in_bucket"] = sizes
            data["days_covered"] = covered
            data["partial"] = [
                c < n or any(_bucket(d, p.granularity) == b for d in plan.partial_dates)
                for b, c, n in zip(buckets, covered, sizes, strict=True)]
            data["daily_avg"] = [None if not c or t is None else round(t / c, 1)
                                 for t, c in zip(total, covered, strict=True)]
        return {
            "data": data,
            "meta": {
                "class_slug": p.class_slug,
                "rule_version": p.rule_version,
                "tz": p.tz,
                "granularity": p.granularity,
                "start_date": p.start.isoformat(),
                "end_date": p.end.isoformat(),
                "platforms": list(p.platforms),
                "data_through": _iso_z(data_through),
                # 从各平台最新事件推断，不是采集侧心跳：平台真的整天零销量时会被误判成 null
                "data_through_by_platform": {src: _iso_z(t) for src, t in (through_by or {}).items()},
                "partial_dates": sorted(d.isoformat() for d in plan.partial_dates),
                "source_tz_assumed": source_tz_assumed,
                "warnings": warnings or [],
                "sum": sums,
                "plan": [seg.describe() for seg in (plan.segments if segments is None else segments)],
                "cache": "miss",
                "stale": False,
                "generated_at": _iso_z(datetime.now(UTC)),
            },
        }
