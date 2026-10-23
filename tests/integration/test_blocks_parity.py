"""月块对拍：真 ClickHouse 样例库 + 真 Redis（make up && make seed）。

经月块拼装的 data 与绕过缓存直接计算的 data 必须逐字段相等。月块用真 BlockCache（MGET / pipeline SET、JSON
序列化），data_gen 用一个本测试独有的代号，不碰 API 正在用的块，测完删掉。
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections import Counter
from datetime import date, timedelta

import pytest
from test_golden import _unavailable, store  # noqa: F401 —— 复用同一个 store fixture 与「默认失败」口径（本目录不是包）

from salestrend.api.params import parse_trend_params
from salestrend.service import TrendService
from salestrend.settings import get_settings

pytestmark = pytest.mark.integration
PLATFORMS = ["app", "marketplace", "website"]
TZS = ["UTC", "Asia/Shanghai", "America/New_York", "Europe/London", "Asia/Kolkata", "America/Los_Angeles",
       "Asia/Kathmandu", "Australia/Eucla", "Australia/Lord_Howe", "Europe/Berlin"]
EDGES = [
    ("UTC", "2023-12-20", "2024-01-10"),              # 跨年（数据从 01-01 起：12 月整月是 0 还是 null 也一并对拍）
    ("Asia/Shanghai", "2024-01-25", "2024-02-05"),    # 跨月
    ("America/New_York", "2024-03-08", "2024-03-12"),  # 03-10 夏令时：23 小时
    ("Asia/Kathmandu", "2024-02-20", "2024-03-13"),   # +05:45，含当天未封口
    ("Australia/Lord_Howe", "2024-02-27", "2024-03-02"),  # +11:00 夏令时、只拨 30 分钟的时区，含闰日
    ("UTC", "2024-03-09", "2024-03-13"),              # marketplace / website 截止之后为 null，当天未封口
    ("UTC", "2024-03-14", "2024-04-20"),              # 整段在所有平台截止之后
    ("Asia/Kolkata", "2024-01-01", "2024-03-13"),     # +05:30 预设时区，日表 + 当天切开
]


class Gen:
    """第一层永远未命中、data_gen 固定为测试专用代号：逼每个请求都走月块。"""

    def __init__(self, gen: str):
        self.gen = gen

    async def data_gen(self):
        return self.gen

    async def get(self, key, stale=False):
        return None

    async def set(self, key, value, ttl_s):
        return None


def P(start: str, end: str, plats, tz, gran):
    return parse_trend_params(class_slug="maxi-dress", rule_version="v1", start_date=start, end_date=end,
                              platforms=plats, tz=tz, granularity=gran, known_platforms=PLATFORMS, max_days=366)


def requests_to_check() -> list:
    rnd = random.Random(7)
    out = [P(s, e, pl, tz, g) for tz, s, e in EDGES for pl in ("app", "marketplace,website", None)
           for g in ("day", "week", "month")]
    for _ in range(150):
        a = date(2023, 12, 1) + timedelta(days=rnd.randrange(135))
        b = a + timedelta(days=rnd.choice([0, 3, 10, 30, 60, 100]) + rnd.randrange(3))
        out.append(P(a.isoformat(), b.isoformat(), ",".join(rnd.sample(PLATFORMS, rnd.randint(1, 3))),
                     rnd.choice(TZS), rnd.choice(["day", "day", "week", "month"])))
    return out


def test_blocks_match_direct_on_real_clickhouse(store):  # noqa: F811
    import redis.asyncio as aioredis

    from salestrend.cache.blocks import BlockCache

    s = get_settings()
    gen = f"test-{uuid.uuid4().hex[:8]}"

    async def go():
        r = aioredis.from_url(s.redis_url, socket_timeout=1, socket_connect_timeout=1)
        try:
            await r.ping()
        except Exception as e:  # noqa: BLE001
            _unavailable(f"Redis not reachable ({type(e).__name__})")
        via = TrendService(s, store, Gen(gen), blocks=BlockCache(r))
        direct = TrendService(s, store, None)
        tags = Counter()
        try:
            for p in requests_to_check():
                got, _ = await via.trend(p)
                want, _ = await direct.trend(p)
                assert got["data"] == want["data"], p
                assert got["meta"]["sum"] == want["meta"]["sum"], p
                assert got["meta"]["partial_dates"] == want["meta"]["partial_dates"], p
                tags[got["meta"]["cache"]] += 1
            n = len([k async for k in r.scan_iter(f"blk:*:{gen}:*")])
        finally:
            keys = [k async for k in r.scan_iter(f"blk:*:{gen}:*")]
            if keys:
                await r.delete(*keys)
            await r.aclose()
        return tags, n

    tags, n = asyncio.run(go())
    assert tags["block"] > 20 and tags["block_partial"] > 20, tags
    assert 0 < n <= len(TZS) * 3 * 5  # 至多 10 个时区 × 3 平台 × 12–3 月 5 个月
