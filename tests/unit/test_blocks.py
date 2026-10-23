"""月块缓存（第二层）：拼装结果与直接计算逐字段对拍，以及失效、降级、singleflight、meta.cache。

假存储按事件模拟 run_plan：每段按 [utc_from, utc_to) 取事件、按请求时区换成本地日累加，和真 SQL 同一个切法。
所以一天被封口线切成两段（前半天品类表、尾巴明细）、夏令时 23 / 25 小时的日、+05:45 这类时区都会真实出现。
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_api import FakeCache  # tests/unit 不是包，pytest 把本目录放进 sys.path

from salestrend.api.errors import Problem
from salestrend.api.params import parse_trend_params
from salestrend.cache.blocks import block_key, last_sealed_day
from salestrend.service import TrendService
from salestrend.settings import get_settings

PLATFORMS = ["app", "marketplace", "website"]
SEALED = datetime(2024, 3, 13, 9, 0, tzinfo=UTC)
THROUGH = {"app": datetime(2024, 3, 13, 9, 45, tzinfo=UTC),          # 当天未封口的尾巴有数据
           "marketplace": datetime(2024, 3, 11, 15, 15, tzinfo=UTC),  # 截止早于封口线：已封口的日也会是 null
           "website": datetime(2024, 3, 12, 18, 5, tzinfo=UTC)}
# 夏令时：New_York 2023-11-05 / 2024-03-10，London 2023-10-29，Lord_Howe 2023-10-01（只拨 30 分钟）、2024-04-07；
# 非整点：Kolkata +05:30（预设，走日表）、Kathmandu +05:45、Eucla +08:45（不预设，走 15 分钟表）
TZS = ["UTC", "Asia/Shanghai", "America/New_York", "Europe/London", "Asia/Kolkata", "America/Los_Angeles",
       "Asia/Kathmandu", "Australia/Eucla", "Australia/Lord_Howe", "Europe/Berlin"]


def make_events(seed: int = 7) -> list[tuple[datetime, str, int]]:
    """2023-09-20 起到各平台截止的稀疏事件：约四分之一的小时有成交，大量日子是 0。"""
    rnd = random.Random(seed)
    out = []
    for src, through in THROUGH.items():
        t = datetime(2023, 9, 20, tzinfo=UTC)
        while t <= through:
            if rnd.random() < 0.25:
                out.append((t + timedelta(minutes=rnd.randrange(60)), src, rnd.randint(1, 3)))
            t += timedelta(hours=rnd.choice([1, 1, 1, 7]))  # 偶尔跳 7 小时，拉出更多零销量日
        out.append((through, src, 1))  # 截止时刻本身有一单：与「截止 = 最新事件」一致
    return [e for e in out if e[0] <= THROUGH[e[1]]]


EVENTS = make_events()
STATE = {"sealed_until": SEALED, "data_through": max(THROUGH.values()),
         **{f"data_through:{src}": t for src, t in THROUGH.items()}}


class EventStore:
    def __init__(self, events=EVENTS, state=None):
        self.events, self.st, self.calls, self.fail, self.delay = events, dict(state or STATE), 0, None, 0.0

    def state(self):
        return dict(self.st)

    def run_plan(self, plan, cls, rv, tz_name, platforms):
        import time

        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        tz, out = ZoneInfo(tz_name), defaultdict(int)
        for seg in plan.segments:
            for ts, src, qty in self.events:
                if src in platforms and seg.utc_from <= ts < seg.utc_to:
                    d = ts.astimezone(tz).date()
                    if seg.first <= d <= seg.last:
                        out[(d, src)] += qty
        return out


class FakeBlocks:
    """内存版 BlockCache：值过一遍 JSON，和真 Redis 一样只剩 JSON 能表达的类型。"""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.fail = False
        self.mgets = 0

    async def mget(self, keys):
        self.mgets += 1
        if self.fail:
            return [None] * len(keys)  # 与 BlockCache 一致：Redis 故障当作全部未命中
        return [json.loads(self.kv[k]) if k in self.kv else None for k in keys]

    async def mset(self, items):
        if not self.fail:
            self.kv.update({k: json.dumps(v) for k, v in items.items()})


class NoL1(FakeCache):
    """第一层永远未命中：对拍要逼每个请求都走月块。"""

    async def get(self, key, stale=False):
        return None


def params(start: date, end: date, platforms=None, tz="UTC", granularity="day"):
    return parse_trend_params(class_slug="maxi-dress", rule_version="v1", start_date=start.isoformat(),
                              end_date=end.isoformat(), platforms=platforms, tz=tz, granularity=granularity,
                              known_platforms=PLATFORMS, max_days=366)


def services(store=None, blocks=None, cache=None):
    s = get_settings()
    store = store or EventStore()
    via = TrendService(s, store, cache or NoL1(), blocks=blocks if blocks is not None else FakeBlocks())
    direct = TrendService(s, EventStore(store.events, store.st), None)  # 不经任何缓存
    return via, direct


def run(coro):
    return asyncio.run(coro)


def random_params(rnd: random.Random):
    start = date(2023, 9, 15) + timedelta(days=rnd.randrange(215))   # 到 2024-04-17：覆盖截止之后
    end = start + timedelta(days=rnd.choice([0, 1, 6, 13, 30, 45, 89, 120, 200]) + rnd.randrange(3))
    k = rnd.randint(1, 3)
    plats = ",".join(rnd.sample(PLATFORMS, k))
    return params(start, end, plats, rnd.choice(TZS), rnd.choice(["day", "day", "week", "month"]))


def test_random_requests_match_direct_computation():
    """核心对拍：600 个随机请求经月块拼装，data 与直接计算逐字段相等；sum / partial_dates 也相等。"""
    rnd = random.Random(20241023)
    via, direct = services()
    seen = defaultdict(int)

    async def go():
        for _ in range(600):
            p = random_params(rnd)
            a, _ = await via.trend(p)
            b, _ = await direct.trend(p)
            assert a["data"] == b["data"], (p, a["meta"]["cache"])
            for f in ("sum", "partial_dates", "data_through_by_platform", "data_through"):
                assert a["meta"][f] == b["meta"][f], (p, f)
            seen[a["meta"]["cache"]] += 1

    run(go())
    assert seen["block"] > 50 and seen["block_partial"] > 50 and seen["miss"] > 0, dict(seen)


@pytest.mark.parametrize("tz,start,end", [
    ("UTC", date(2023, 12, 20), date(2024, 1, 10)),                   # 跨年
    ("Asia/Shanghai", date(2024, 1, 25), date(2024, 2, 5)),           # 跨月
    ("America/New_York", date(2024, 3, 8), date(2024, 3, 12)),        # 03-10 春季拨快：23 小时
    ("America/New_York", date(2023, 11, 3), date(2023, 11, 7)),       # 11-05 秋季拨回：25 小时
    ("Europe/London", date(2023, 10, 27), date(2023, 10, 31)),        # 10-29 拨回
    ("Australia/Lord_Howe", date(2023, 9, 29), date(2023, 10, 3)),    # 10-01 只拨 30 分钟
    ("Asia/Kathmandu", date(2024, 2, 20), date(2024, 3, 13)),         # +05:45，含当天未封口
    ("Australia/Eucla", date(2024, 2, 27), date(2024, 3, 2)),         # +08:45，闰日
    ("UTC", date(2024, 3, 9), date(2024, 3, 13)),                     # 部分平台 null + 当天未封口
    ("UTC", date(2024, 3, 14), date(2024, 4, 20)),                    # 整段在所有平台截止之后
    ("Asia/Shanghai", date(2024, 3, 13), date(2024, 3, 13)),          # 只有未封口的当天
])
@pytest.mark.parametrize("granularity", ["day", "week", "month"])
def test_edge_cases_match_twice(tz, start, end, granularity):
    """每个边界跑两遍：第一遍回填（miss / block_partial），第二遍从块拼（block / block_partial），都与直接计算相等。"""
    via, direct = services()
    for plats in ("app", "marketplace,website", None):
        p = params(start, end, plats, tz, granularity)

        async def go(p=p):
            want, _ = await direct.trend(p)
            for _ in range(2):
                got, _ = await via.trend(p)
                assert got["data"] == want["data"]
                assert got["meta"]["sum"] == want["meta"]["sum"]
                assert got["meta"]["partial_dates"] == want["meta"]["partial_dates"]

        run(go())


def test_cache_tags_and_plan_lists_only_queried_segments():
    store = EventStore()
    via, _ = services(store=store)

    async def go():
        a, _ = await via.trend(params(date(2024, 1, 5), date(2024, 1, 20)))   # 冷：整月回填
        assert a["meta"]["cache"] == "miss" and store.calls == 1
        (seg,) = a["meta"]["plan"]
        assert (seg["segment"], seg["from"], seg["to"]) == ("class_daily", "2024-01-01", "2024-01-31")  # 回填整月
        b, _ = await via.trend(params(date(2024, 1, 2), date(2024, 1, 31), "app,website"))
        assert b["meta"]["cache"] == "block" and b["meta"]["plan"] == [] and store.calls == 1  # 不查库
        c, _ = await via.trend(params(date(2024, 1, 20), date(2024, 3, 13)))  # 1 月有块；2、3 月缺；当天现查
        assert c["meta"]["cache"] == "block_partial"
        segs = [(s["segment"], s["from"], s["to"]) for s in c["meta"]["plan"]]
        # 缺的 2、3 月合并成一段日表；当天按封口线切：前半天 15 分钟表、尾巴明细
        assert segs == [("class_daily", "2024-02-01", "2024-03-12"), ("class_15m", "2024-03-13", "2024-03-13"),
                        ("fact", "2024-03-13", "2024-03-13")]
        d, _ = await via.trend(params(date(2024, 3, 13), date(2024, 3, 13)))  # 只有未封口的日：用不上块
        assert d["meta"]["cache"] == "miss"
        e, _ = await via.trend(params(date(2024, 3, 1), date(2024, 3, 13)))
        assert e["meta"]["cache"] == "block_partial" and {s["segment"] for s in e["meta"]["plan"]} == {
            "class_15m", "fact"}

    run(go())


def test_block_stores_null_and_zero_distinctly():
    blocks = FakeBlocks()
    via, _ = services(blocks=blocks)
    run(via.trend(params(date(2024, 3, 1), date(2024, 3, 12), "marketplace")))
    blk = json.loads(blocks.kv[block_key("maxi-dress", "v1", "0", "UTC", "marketplace", date(2024, 3, 1))])
    assert len(blk["v"]) == 12                     # 只放已封口的日：03-13 未封口
    assert blk["v"][11] is None and blk["v"][10] is not None  # 03-12 晚于 marketplace 截止；03-11 含截止
    assert blk["o"] == 1 and blk["t"] == "2024-03-11T15:15:00Z"
    via2, _ = services(blocks=blocks)
    run(via2.trend(params(date(2024, 1, 1), date(2024, 1, 31), "app")))
    jan = json.loads(blocks.kv[block_key("maxi-dress", "v1", "0", "UTC", "app", date(2024, 1, 1))])
    assert 0 in jan["v"] and None not in jan["v"] and jan["o"] == 0


def test_sealed_until_advance_without_new_gen_refills_month():
    """封口线前移而 data_gen 没变（只跑了重算）：当月块只封到前几天，整月重填，结果仍与直接计算相等。"""
    store = EventStore(state={**STATE, "sealed_until": datetime(2024, 3, 5, tzinfo=UTC)})
    blocks = FakeBlocks()
    via, _ = services(store=store, blocks=blocks)
    run(via.trend(params(date(2024, 3, 1), date(2024, 3, 4), "app")))
    store.st["sealed_until"] = SEALED
    via2, direct = services(store=store, blocks=blocks)
    p = params(date(2024, 3, 1), date(2024, 3, 10), "app")
    got, want = run(via2.trend(p))[0], run(direct.trend(p))[0]
    assert got["data"] == want["data"] and got["meta"]["cache"] == "miss"  # 旧块不够长，不算用上
    blk = json.loads(blocks.kv[block_key("maxi-dress", "v1", "0", "UTC", "app", date(2024, 3, 1))])
    assert len(blk["v"]) == 12


def test_platform_cutoff_advance_without_new_gen_invalidates_open_blocks():
    """平台截止前移而 data_gen 没变：含 null / 不完整日的块作废重填，不会把迟到的数据当成 null。"""
    early = {**STATE, "data_through:marketplace": datetime(2024, 3, 5, 12, tzinfo=UTC)}
    store = EventStore(state=early)
    blocks = FakeBlocks()
    via, _ = services(store=store, blocks=blocks)
    p = params(date(2024, 3, 1), date(2024, 3, 12), "marketplace")
    run(via.trend(p))
    store.st = dict(STATE)
    via2, direct = services(store=store, blocks=blocks)
    got, want = run(via2.trend(p))[0], run(direct.trend(p))[0]
    assert got["data"] == want["data"] and got["meta"]["cache"] == "miss"
    # 已完结的块（1 月整月）不受影响
    run(via2.trend(params(date(2024, 1, 1), date(2024, 1, 31), "marketplace")))
    calls = store.calls
    assert run(via2.trend(params(date(2024, 1, 3), date(2024, 1, 9), "marketplace")))[0]["meta"]["cache"] == "block"
    assert store.calls == calls


def test_new_data_gen_ignores_old_blocks():
    class Gen(NoL1):
        gen = "0"

        async def data_gen(self):
            return self.gen

    cache, blocks, store = Gen(), FakeBlocks(), EventStore()
    via = TrendService(get_settings(), store, cache, blocks=blocks)
    p = params(date(2024, 1, 1), date(2024, 1, 31))
    assert run(via.trend(p))[0]["meta"]["cache"] == "miss"
    assert run(via.trend(p))[0]["meta"]["cache"] == "block"
    cache.gen = "1"  # 回填后 data_gen 递增
    assert run(via.trend(p))[0]["meta"]["cache"] == "miss"


def test_redis_down_both_layers_miss_and_still_correct():
    """月块 MGET 失败与读不到 data_gen 两种故障：都当作未命中直接查库，结果正确。"""
    blocks = FakeBlocks()
    blocks.fail = True
    via, direct = services(blocks=blocks)
    p = params(date(2024, 1, 1), date(2024, 3, 13), "app,website", "Asia/Kathmandu", "week")
    for _ in range(2):
        got = run(via.trend(p))[0]
        assert got["meta"]["cache"] == "miss" and got["data"] == run(direct.trend(p))[0]["data"]

    class GenDown(FakeCache):
        async def data_gen(self):
            return None

    store, cache = EventStore(), GenDown()
    svc = TrendService(get_settings(), store, cache, blocks=FakeBlocks())
    for _ in range(2):
        assert run(svc.trend(p))[0]["meta"]["cache"] == "miss"
    assert store.calls == 2 and not cache.kv and not svc.blocks.kv  # 每次整段查一次库；两层都不读不写


def test_storage_down_blocks_still_serve_fully_sealed_range():
    """存储故障：区间全部由月块覆盖时不需要查库（水位在进程内缓存 5 秒），照常返回新鲜数据。"""
    store = EventStore()
    via, _ = services(store=store)
    run(via.trend(params(date(2024, 1, 1), date(2024, 2, 29))))
    store.fail = ConnectionError("down")
    got = run(via.trend(params(date(2024, 1, 10), date(2024, 2, 10))))[0]
    assert got["meta"]["cache"] == "block" and got["meta"]["stale"] is False


def test_storage_down_with_missing_block_falls_back_to_stale_copy():
    """缺块要查库而存储挂了：仍走第一层的 stale 副本；月块本身不存 stale 副本。"""
    cache, blocks, store = FakeCache(), FakeBlocks(), EventStore()
    svc = TrendService(get_settings(), store, cache, blocks=blocks)
    p = params(date(2024, 1, 1), date(2024, 1, 31))
    run(svc.trend(p))
    del cache.kv[next(k for k in cache.kv if not k.startswith("stale:"))]
    blocks.kv.clear()
    store.fail = ConnectionError("down")
    got = run(svc.trend(p))[0]
    assert got["meta"]["cache"] == "stale" and got["meta"]["stale"] is True
    with pytest.raises(Problem):  # 没有 stale 副本的 key：503，与只有一层缓存时相同
        run(svc.trend(params(date(2024, 2, 1), date(2024, 2, 9))))


def test_concurrent_overlapping_requests_fill_each_block_once():
    """50 个不同区间（不同的第一层 key）并发落在 1–2 月：每个月块只回源一次，等待者复用。"""
    store = EventStore()
    store.delay = 0.2
    blocks = FakeBlocks()
    svc = TrendService(get_settings(), store, NoL1(), blocks=blocks)
    rnd = random.Random(1)
    ps = []
    for _ in range(50):
        a = date(2024, 1, 1) + timedelta(days=rnd.randrange(50))
        ps.append(params(a, a + timedelta(days=rnd.randrange(8))))

    async def burst():
        return await asyncio.gather(*[svc.trend(p) for p in ps])

    results = run(burst())
    assert store.calls <= 2 and svc.block_flight.calls <= 2  # 第一个请求认领它要的块，别的请求顶多再认领一次
    assert {r[0]["meta"]["cache"] for r in results} <= {"miss", "block"}
    _, direct = services()
    for p, (body, _) in zip(ps, results, strict=True):
        assert body["data"] == run(direct.trend(p))[0]["data"]


def test_api_counts_block_labels():
    from fastapi.testclient import TestClient
    from prometheus_client import REGISTRY
    from test_api import URL

    from salestrend.api.app import create_app

    def val(cache):
        return REGISTRY.get_sample_value("trend_requests_total", {"cache": cache, "status": "200"}) or 0.0

    app = create_app(settings=get_settings(), store=EventStore(), cache=NoL1(), use_redis=False, blocks=FakeBlocks())
    c = TestClient(app)
    before = {k: val(k) for k in ("miss", "block", "block_partial")}
    r1 = c.get(URL, params={"start_date": "2024-01-01", "end_date": "2024-01-31"})
    r2 = c.get(URL, params={"start_date": "2024-01-05", "end_date": "2024-01-25"})
    r3 = c.get(URL, params={"start_date": "2024-01-05", "end_date": "2024-03-13"})
    assert [r.headers["X-Cache"] for r in (r1, r2, r3)] == ["miss", "block", "block_partial"]
    assert r2.headers["X-Query-Plan"] == ""
    assert {k: val(k) - before[k] for k in before} == {"miss": 1, "block": 1, "block_partial": 1}
    lat = REGISTRY.get_sample_value("trend_request_seconds_count", {"path": "block"})
    assert lat and lat >= 1


@pytest.mark.parametrize("tz", ["UTC", "Asia/Kathmandu", "America/New_York", "Australia/Lord_Howe"])
def test_last_sealed_day_agrees_with_planner(tz):
    from salestrend.query.planner import local_day_utc

    z = ZoneInfo(tz)
    t = datetime(2023, 9, 25, tzinfo=UTC)
    while t < datetime(2024, 4, 10, tzinfo=UTC):
        d = last_sealed_day(z, t)
        assert local_day_utc(d, z)[1] <= t < local_day_utc(d + timedelta(days=1), z)[1]
        t += timedelta(minutes=15 * 37)  # 跨过所有夏令时切换、步长与日界错开


def test_block_cache_disabled_by_setting():
    from salestrend.api.app import create_app

    s = replace(get_settings(), block_cache=False)
    app = create_app(settings=s, store=EventStore())
    assert app.state.service.blocks is None
