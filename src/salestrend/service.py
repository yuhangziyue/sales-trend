"""趋势查询服务：缓存 → singleflight → 规划 → 执行 → 合并补零 → 组装响应。存储故障时降级到旧缓存。"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from datetime import UTC, date, datetime, timedelta

from .api.errors import Problem
from .api.params import TrendParams
from .cache.resilience import CircuitBreaker, ResponseCache, SingleFlight
from .query.planner import plan_query
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
    def __init__(self, s: Settings, store, cache: ResponseCache | None, store_factory=None):
        self.s, self.store, self.cache, self._factory = s, store, cache, store_factory
        self.flight = SingleFlight()
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
        data_gen = await self.cache.data_gen() if self.cache else "0"
        key = p.cache_key(data_gen)
        if self.cache and (hit := await self.cache.get(key)):
            hit["meta"]["cache"] = "hit"
            self._refresh_tz_meta(hit, p)
            return hit, TTL_SEALED_S if hit["meta"].pop("_sealed", False) else TTL_OPEN_S

        try:
            body, ttl = await self.flight.do(key, lambda: self._compute(p, key))
        except Problem as e:
            if e.code in ("UPSTREAM_UNAVAILABLE", "QUERY_TIMEOUT") and self.cache and (
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

    async def _compute(self, p: TrendParams, key: str) -> tuple[dict, int]:
        st = await self.pipeline_state()
        # 各平台截止：rollup 写的 data_through:{src}；旧水位里没有时退回全局截止
        through_by = {src: st.get(f"data_through:{src}", st.get("data_through")) for src in p.platforms}
        plan = plan_query(p.start, p.end, p.tz, sealed_until=st.get("sealed_until"),
                          data_through=st.get("data_through"), preset_timezones=self.s.preset_timezones,
                          platform_through=through_by)
        cells = await self._call_store("run_plan", plan, p.class_slug, p.rule_version, p.tz,
                                       list(p.platforms))
        assumed, warnings = source_tz_meta(self.s, p.platforms, p.tz)
        body = self.assemble(p, plan, cells, st.get("data_through"), through_by=through_by,
                             source_tz_assumed=assumed, warnings=warnings)
        ttl = TTL_SEALED_S if plan.fully_sealed else TTL_OPEN_S
        if self.cache:
            await self.cache.set(key, {**body, "meta": {**body["meta"], "_sealed": plan.fully_sealed}}, ttl)
        body["meta"]["cache"] = "miss"
        return body, ttl

    @staticmethod
    def assemble(p: TrendParams, plan, cells: dict, data_through: datetime | None, *,
                 through_by: dict[str, datetime | None] | None = None, source_tz_assumed: bool = False,
                 warnings: list[dict] | None = None) -> dict:
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
            out = []
            for b in buckets:
                xs = [v for d, v in vals.items() if _bucket(d, p.granularity) == b]
                known = [x for x in xs if x is not None]
                out.append(sum(known) if known else None)
            return out

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
                "plan": [seg.describe() for seg in plan.segments],
                "cache": "miss",
                "stale": False,
                "generated_at": _iso_z(datetime.now(UTC)),
            },
        }
