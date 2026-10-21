"""趋势查询服务：缓存 → singleflight → 规划 → 执行 → 合并补零 → 组装响应。存储故障时降级到旧缓存。"""

from __future__ import annotations

import asyncio
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
            return hit, TTL_SEALED_S if hit["meta"].pop("_sealed", False) else TTL_OPEN_S

        try:
            body, ttl = await self.flight.do(key, lambda: self._compute(p, key))
        except Problem as e:
            if e.code in ("UPSTREAM_UNAVAILABLE", "QUERY_TIMEOUT") and self.cache and (
                stale := await self.cache.get(key, stale=True)
            ):
                stale["meta"].pop("_sealed", None)
                stale["meta"].update(cache="stale", stale=True)
                return stale, 0
            raise
        body = {"data": body["data"], "meta": {**body["meta"]}}
        body["meta"].pop("_sealed", None)
        return body, ttl

    async def _compute(self, p: TrendParams, key: str) -> tuple[dict, int]:
        st = await self.pipeline_state()
        plan = plan_query(p.start, p.end, p.tz, sealed_until=st.get("sealed_until"),
                          data_through=st.get("data_through"), preset_timezones=self.s.preset_timezones)
        cells = await self._call_store("run_plan", plan, p.class_slug, p.rule_version, p.tz,
                                       list(p.platforms))
        body = self.assemble(p, plan, cells, st.get("data_through"))
        ttl = TTL_SEALED_S if plan.fully_sealed else TTL_OPEN_S
        if self.cache:
            await self.cache.set(key, {**body, "meta": {**body["meta"], "_sealed": plan.fully_sealed}}, ttl)
        body["meta"]["cache"] = "miss"
        return body, ttl

    @staticmethod
    def assemble(p: TrendParams, plan, cells: dict, data_through: datetime | None) -> dict:
        # 按日补零：没卖出去 = 0；晚于数据截止 = null。前端画折线时 null 断开、0 落地，含义不同
        daily: dict[str, dict[date, int | None]] = {
            src: {d: (None if d in plan.null_dates else cells.get((d, src), 0)) for d in plan.days}
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
        return {
            "data": {"dates": [d.isoformat() for d in labels], "series": series, "total": total},
            "meta": {
                "class_slug": p.class_slug,
                "rule_version": p.rule_version,
                "tz": p.tz,
                "granularity": p.granularity,
                "start_date": p.start.isoformat(),
                "end_date": p.end.isoformat(),
                "platforms": list(p.platforms),
                "data_through": _iso_z(data_through),
                "partial_dates": sorted(d.isoformat() for d in plan.partial_dates),
                "sum": sums,
                "plan": [seg.describe() for seg in plan.segments],
                "cache": "miss",
                "stale": False,
                "generated_at": _iso_z(datetime.now(UTC)),
            },
        }
