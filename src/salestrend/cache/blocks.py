"""第二层缓存：月块。一个块 = （品类, 规则版本, data_gen, 时区, 单个平台, 本地年月）这个月里已封口各日的销量。

为什么要有这一层：第一层按「整个响应」缓存，参数差一点就是另一个 key，任意区间压测（P6）命中率只有 11.2%。
任意两个区间只要落在同一个月、同一个时区，就共享同一批月块，响应由月块拼出来。

为什么按月：
- 比按日 key 少 30 倍，一年区间一次 MGET 取 12 × 平台数个 key，而不是 366 × 平台数个；
- 比按年回填粒度合适：缺一块只补一个月，一个月的日表段查询与查几天同一量级（ClickHouse 按 granule 读）；
  按年的话，查 3 天就要顺带回填 365 天，「当月还在封口」的块也要整年反复重写。
为什么按单个平台：平台组合有 7 种，按组合存要存 7 份且彼此不能复用；按单个平台存，任何组合都由 1–3 个块拼出，
总量只有组合方式的 3/7，而 total、days_covered 这些跨平台的口径交给 assemble 现算。

只放已封口的日（本地日结束时刻 ≤ sealed_until）：已封口的汇总表只会被回填改写，回填会递增 data_gen，
key 里带 data_gen，块自然作废，所以不靠 TTL 失效。TTL 7 天（±10% 抖动）只为回收旧 data_gen 留下的块。

值：{"v": [当月 1 号起逐日的值], "t": 填块时该平台的截止时间, "o": 是否含该平台尚未完结的日}。
v 里 0 = 没卖出，null = 晚于该平台的截止。截止时间来自水位，不在 key 里：水位前移而 data_gen 没变时
（比如平台迟到的数据进了未封口区），含 null 或不完整日的块（o = 1）必须作废，否则会把迟到的数据当成 null / 少算。
"""

from __future__ import annotations

import calendar
import json
import logging
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..query.planner import local_day_utc

log = logging.getLogger(__name__)

BLOCK_SCHEMA = "b1"            # 块结构版本：改了 value 的格式就递增，旧块随之作废
BLOCK_TTL_S = 7 * 24 * 3600    # 只为回收内存；失效靠 key 里的 data_gen


def block_key(cls: str, rv: str, data_gen: str, tz: str, src: str, month: date) -> str:
    return ":".join(["blk", BLOCK_SCHEMA, cls, rv, data_gen, tz, src, f"{month:%Y-%m}"])


def month_of(d: date) -> date:
    return d.replace(day=1)


def month_end(m: date) -> date:
    return m.replace(day=calendar.monthrange(m.year, m.month)[1])


def next_month(m: date) -> date:
    return (m + timedelta(days=32)).replace(day=1)


def last_sealed_day(tz: ZoneInfo, sealed_until: datetime | None) -> date | None:
    """本地日结束时刻 ≤ sealed_until 的最后一天。与规划器同一个判定（local_day_utc），夏令时切换日也一致。"""
    if sealed_until is None:
        return None
    d = sealed_until.astimezone(tz).date()
    while local_day_utc(d, tz)[1] > sealed_until:  # 至多退两步：封口线所在那天未封口
        d -= timedelta(days=1)
    return d


def iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def encode(values: list[int | None], through: datetime | None, tz: ZoneInfo, last_day: date) -> dict:
    # o：块里有没有「对这个平台还没完结」的日（结束时刻晚于它的截止）。逐日单调，只看最后一天就够
    return {"v": values, "t": iso(through),
            "o": int(through is None or local_day_utc(last_day, tz)[1] > through)}


def usable(blk: dict | None, need: list[date], through: datetime | None) -> bool:
    """块能不能直接用：覆盖到所需的最后一天；含未完结日时平台截止没变；所需的日在块里都不是 null。

    最后一条在截止没变时是冗余的（null 的判定同源），留着当保险：拼错一个数比多查一次库贵得多。
    """
    if not blk or len(blk["v"]) < max(d.day for d in need):
        return False  # 封口线前移而 data_gen 没变：当月块只封到前几天，整月重填
    if blk["o"] and blk["t"] != iso(through):
        return False
    return all(blk["v"][d.day - 1] is not None for d in need)


class BlockCache:
    """月块的 Redis 读写。与 ResponseCache 共用连接；任何 Redis 故障都当作全部未命中，不影响正确性。

    不写 stale 副本：月块不会因为时间过期而变旧，只会因为 data_gen 递增而作废；存储故障时第一层的
    stale 副本已经兜底，再给月块存一份旧代号的副本，等于在 data_gen 之外再维护一套「上一代」索引，内存翻倍。
    """

    def __init__(self, r):
        self.r = r

    async def mget(self, keys: list[str]) -> list[dict | None]:
        if not keys:
            return []
        try:
            raws = await self.r.mget(keys)  # 一次往返取回整个请求要的块
            return [json.loads(x) if x else None for x in raws]
        except Exception as e:  # noqa: BLE001 —— Redis 任何故障都降级为未命中
            log.warning("block mget failed: %s", e)
            return [None] * len(keys)

    async def mset(self, items: dict[str, dict]) -> None:
        if not items:
            return
        try:
            async with self.r.pipeline(transaction=False) as p:
                for k, v in items.items():
                    p.set(k, json.dumps(v, separators=(",", ":")), ex=int(BLOCK_TTL_S * random.uniform(0.9, 1.1)))
                await p.execute()
        except Exception as e:  # noqa: BLE001
            log.warning("block mset failed: %s", e)
