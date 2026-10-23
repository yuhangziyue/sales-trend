"""缓存、singleflight、令牌桶、熔断。

Redis 挂了不影响正确性：缓存读写失败一律当未命中，直接查存储。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from ..metrics import CIRCUIT_OPEN, SINGLEFLIGHT_SHARED

log = logging.getLogger(__name__)

STALE_TTL_S = 7 * 24 * 3600


class ResponseCache:
    """cache-aside。每个 key 同时写一份长 TTL 的 stale 副本，存储故障时用来降级。"""

    def __init__(self, url: str):
        self.r = aioredis.from_url(url, socket_timeout=0.3, socket_connect_timeout=0.3)

    async def get(self, key: str, stale: bool = False) -> dict | None:
        try:
            raw = await self.r.get(("stale:" if stale else "") + key)
            return json.loads(raw) if raw else None
        except Exception as e:  # noqa: BLE001 —— Redis 任何故障都降级为未命中
            log.warning("cache get failed: %s", e)
            return None

    async def set(self, key: str, value: dict, ttl_s: int) -> None:
        jittered = int(ttl_s * random.uniform(0.9, 1.1))  # ±10% 抖动，防同时过期雪崩
        payload = json.dumps(value, separators=(",", ":"))
        try:
            async with self.r.pipeline(transaction=False) as p:
                p.set(key, payload, ex=jittered)
                p.set("stale:" + key, payload, ex=STALE_TTL_S)
                await p.execute()
        except Exception as e:  # noqa: BLE001
            log.warning("cache set failed: %s", e)

    async def data_gen(self) -> str | None:
        """读不到（Redis 故障）返回 None，调用方据此两层缓存都不读不写。

        不能退回 "0"：旧代号 0 下可能还留着 7 天 TTL 的月块和 24 小时的整响应，读到就是回填前的旧数据。
        """
        try:
            return (await self.r.get("data_gen") or b"0").decode()
        except Exception:  # noqa: BLE001
            return None

    async def bump_data_gen(self) -> None:
        try:
            await self.r.incr("data_gen")
        except Exception as e:  # noqa: BLE001
            log.warning("bump data_gen failed: %s", e)

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:  # noqa: BLE001
            return False


class SingleFlight:
    """同一个 key 并发未命中时只放第一个请求去查库，其余等它的结果（防缓存击穿）。"""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self.calls = 0  # 实际执行次数，测试与压测观测用

    async def do(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        if key in self._inflight:
            SINGLEFLIGHT_SHARED.inc()  # 被合并掉的请求：不查库，等第一个请求的结果
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            self.calls += 1
            result = await fn()
            fut.set_result(result)
            return result
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # 标记已取走，避免没人等时报 never retrieved
            raise
        finally:
            del self._inflight[key]


class KeyedFlight:
    """按单个 key 合并的 singleflight，用于月块：一个请求要一批 key，只认领别人没在查的那些，其余等别人。

    为什么按块的 key、而不是按「缺块集合」合并：任意区间下两个请求的缺块集合几乎从不相同（1 月 15–20 号与
    1 月 3 号–2 月 9 号缺的块有交集、集合却不同），按集合合并在冷启动时等于没合并；按块合并，交集部分只查一次。
    认领的 key 由本请求一次查完；不会死锁：查库时不等任何人，等别人只发生在自己查完之后。
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self.calls = 0  # 实际回源次数，测试观测用

    async def do(self, keys: list[str], fetch: Callable[[list[str]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        theirs = {k: self._inflight[k] for k in keys if k in self._inflight}
        mine = [k for k in keys if k not in theirs]
        loop = asyncio.get_running_loop()
        futs = {k: loop.create_future() for k in mine}
        self._inflight.update(futs)
        out: dict[str, Any] = {}
        try:
            if mine:
                self.calls += 1
                out = await fetch(mine)
                for k in mine:
                    futs[k].set_result(out.get(k))
        except BaseException as e:
            for f in futs.values():
                if not f.done():
                    f.set_exception(e)
                    f.exception()  # 标记已取走，避免没人等时报 never retrieved
            raise
        finally:
            for k in mine:
                self._inflight.pop(k, None)
        if theirs:
            SINGLEFLIGHT_SHARED.inc()  # 本请求有块是等别人查回来的
            for k, f in theirs.items():
                out[k] = await asyncio.shield(f)
        return out


class TokenBucket:
    """按 API key 的令牌桶，允许看板刷新时的短突发。

    进程内实现：多实例部署时应放到网关或 Redis（Lua 脚本），这里演示算法与 429 语义。
    """

    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.burst = rate_per_s, burst
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """返回 0 表示放行；否则返回建议等待秒数。"""
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.rate


class CircuitBreaker:
    """连续失败 N 次后熔断 cooldown 秒，熔断期间只读缓存，不再打存储。"""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30):
        self.threshold, self.cooldown = threshold, cooldown_s
        self.failures, self.open_until = 0, 0.0

    @property
    def is_open(self) -> bool:
        opened = time.monotonic() < self.open_until
        CIRCUIT_OPEN.set(int(opened))  # 熔断到期自动关闭，没有事件可挂：每次判断时回写指标
        return opened

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.cooldown
            self.failures = 0
            CIRCUIT_OPEN.set(1)
