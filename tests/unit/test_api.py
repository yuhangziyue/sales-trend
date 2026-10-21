"""API 层测试：用假存储，不需要数据库。覆盖 11 个错误码、缓存 key 规范化、降级路径。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from salestrend.api.app import create_app
from salestrend.settings import get_settings

STATE = {"sealed_until": datetime(2024, 3, 13, 9, tzinfo=UTC), "data_through": datetime(2024, 3, 13, 9, 45, tzinfo=UTC)}


class FakeStore:
    def __init__(self, fail: Exception | None = None):
        self.fail, self.calls = fail, 0

    def state(self):
        return STATE

    def run_plan(self, plan, cls, rv, tz, platforms):
        self.calls += 1
        if self.fail:
            raise self.fail
        out = defaultdict(int)
        for seg in plan.segments:
            for d in seg.dates:
                for p in platforms:
                    out[(d, p)] = 1
        return out


class FakeCache:
    """内存版缓存，接口与 ResponseCache 相同。"""

    def __init__(self):
        self.kv: dict[str, dict] = {}

    async def get(self, key, stale=False):
        import copy

        v = self.kv.get(("stale:" if stale else "") + key)
        return copy.deepcopy(v)

    async def set(self, key, value, ttl_s):
        import copy

        self.kv[key] = copy.deepcopy(value)
        self.kv["stale:" + key] = copy.deepcopy(value)

    async def data_gen(self):
        return "0"

    async def ping(self):
        return True


def client(store=None, cache=None, **overrides):
    s = get_settings()
    if overrides:
        from dataclasses import replace

        s = replace(s, **overrides)
    app = create_app(settings=s, store=store or FakeStore(), cache=cache, use_redis=False)
    return TestClient(app), app


URL = "/v1/product-classes/maxi-dress/sales-trend"
OK = {"start_date": "2024-01-01", "end_date": "2024-01-31"}


@pytest.mark.parametrize("params,code,status", [
    ({"start_date": "2024/01/01", "end_date": "2024-01-31"}, "INVALID_DATE", 400),
    ({"start_date": "2023-02-29", "end_date": "2023-03-01"}, "INVALID_DATE", 400),
    ({"end_date": "2024-01-31"}, "INVALID_DATE", 400),
    ({"start_date": "2024-02-01", "end_date": "2024-01-01"}, "RANGE_INVERTED", 400),
    ({"start_date": "2024-01-01", "end_date": "2025-02-01"}, "RANGE_TOO_LARGE", 400),
    ({**OK, "platforms": "app,tiktok"}, "UNKNOWN_PLATFORM", 400),
    ({**OK, "tz": "+08:00"}, "INVALID_TZ", 400),
    ({**OK, "tz": "Mars/Base"}, "INVALID_TZ", 400),
    ({**OK, "granularity": "hour"}, "INVALID_GRANULARITY", 400),
    ({**OK, "rule_version": "v9"}, "RULE_VERSION_NOT_FOUND", 404),
])
def test_param_errors(params, code, status):
    c, _ = client()
    r = c.get(URL, params=params)
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == code and body["status"] == status and body["title"] and body["type"]


def test_class_not_found_does_not_touch_storage():
    store = FakeStore()
    c, _ = client(store=store)
    r = c.get("/v1/product-classes/mini-skirt/sales-trend", params=OK)
    assert r.status_code == 404 and r.json()["code"] == "CLASS_NOT_FOUND"
    assert store.calls == 0  # 防缓存穿透：未知品类不碰存储


def test_rate_limited():
    c, _ = client(rate_per_s=0.001, rate_burst=2)
    codes = [c.get(URL, params=OK, headers={"X-API-Key": "k"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = c.get(URL, params=OK, headers={"X-API-Key": "k"})
    assert r.json()["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) >= 1


def test_upstream_unavailable_without_stale_cache():
    c, _ = client(store=FakeStore(fail=ConnectionError("down")))
    r = c.get(URL, params=OK)
    assert r.status_code == 503 and r.json()["code"] == "UPSTREAM_UNAVAILABLE"


def test_query_timeout_without_stale_cache():
    c, _ = client(store=FakeStore(fail=RuntimeError("Code: 159. Timeout exceeded: elapsed 2.1 seconds")))
    r = c.get(URL, params=OK)
    assert r.status_code == 504 and r.json()["code"] == "QUERY_TIMEOUT"


def test_stale_cache_served_when_storage_down():
    cache, store = FakeCache(), FakeStore()
    c, _ = client(store=store, cache=cache)
    assert c.get(URL, params=OK).status_code == 200
    key = next(k for k in cache.kv if not k.startswith("stale:"))
    del cache.kv[key]  # 新鲜缓存过期，只剩 stale 副本
    store.fail = ConnectionError("down")
    r = c.get(URL, params=OK)
    assert r.status_code == 200
    assert r.json()["meta"]["stale"] is True and r.json()["meta"]["cache"] == "stale"
    assert "110" in r.headers["Warning"]


def test_platform_order_hits_same_cache_key():
    cache, store = FakeCache(), FakeStore()
    c, _ = client(store=store, cache=cache)
    c.get(URL, params={**OK, "platforms": "website,app"})
    r = c.get(URL, params={**OK, "platforms": "app, website,app"})
    assert r.json()["meta"]["cache"] == "hit" and store.calls == 1


def test_null_vs_zero_and_partial_dates():
    c, _ = client()
    r = c.get(URL, params={"start_date": "2024-03-12", "end_date": "2024-03-15"}).json()
    assert r["data"]["dates"] == ["2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15"]
    assert r["data"]["total"][2:] == [None, None]
    assert r["meta"]["partial_dates"] == ["2024-03-13"]
    assert [s["segment"] for s in r["meta"]["plan"]] == ["class_daily", "fact"]


def test_week_granularity_buckets():
    c, _ = client()
    r = c.get(URL, params={"start_date": "2024-01-03", "end_date": "2024-01-15", "granularity": "week",
                           "platforms": "app"}).json()
    # 01-03 是周三：首个桶从 start_date 起；之后按 ISO 周一
    assert r["data"]["dates"] == ["2024-01-03", "2024-01-08", "2024-01-15"]
    assert r["data"]["series"][0]["values"] == [5, 7, 1]


def test_etag_304():
    c, _ = client()
    r = c.get(URL, params=OK)
    r2 = c.get(URL, params=OK, headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304


def test_singleflight_collapses_concurrent_misses():
    from salestrend.api.params import parse_trend_params
    from salestrend.service import TrendService

    class SlowStore(FakeStore):
        def run_plan(self, *a):
            import time

            time.sleep(0.2)
            return super().run_plan(*a)

    store = SlowStore()
    svc = TrendService(get_settings(), store, FakeCache())
    p = parse_trend_params(class_slug="maxi-dress", rule_version="v1", start_date="2024-01-01",
                           end_date="2024-01-31", platforms=None, tz=None, granularity=None,
                           known_platforms=["app", "marketplace", "website"], max_days=366)

    async def burst():
        return await asyncio.gather(*[svc.trend(p) for _ in range(200)])

    results = asyncio.run(burst())
    assert store.calls == 1 and len(results) == 200


def test_list_classes_and_detail():
    c, _ = client()
    r = c.get("/v1/product-classes").json()["data"]
    assert r[0]["class_slug"] == "maxi-dress" and r[0]["default_rule_version"] == "v1"
    assert r[0]["rule_versions"] == ["v1", "v2", "v3"]
    d = c.get("/v1/product-classes/maxi-dress").json()["data"]
    assert d["rules"][0]["id"] == "R2_other_garment"


# ---------- A1 按平台截止 ----------
BY_SRC_STATE = {**STATE, "data_through:app": datetime(2024, 3, 13, 9, 45, tzinfo=UTC),
                "data_through:marketplace": datetime(2024, 3, 11, 15, 15, tzinfo=UTC),
                "data_through:website": datetime(2024, 3, 12, 18, 5, tzinfo=UTC)}


class PerPlatformStore(FakeStore):
    def state(self):
        return BY_SRC_STATE


def test_per_platform_cutoff_null_and_total():
    c, _ = client(store=PerPlatformStore())
    r = c.get(URL, params={"start_date": "2024-03-10", "end_date": "2024-03-15"}).json()
    got = {s["platform"]: s["values"] for s in r["data"]["series"]}
    assert got == {"app": [1, 1, 1, 1, None, None], "marketplace": [1, 1, None, None, None, None],
                   "website": [1, 1, 1, None, None, None]}
    assert r["data"]["total"] == [3, 3, 2, 1, None, None]  # 部分 null：非 null 平台之和；全部 null：null
    assert r["meta"]["partial_dates"] == ["2024-03-11", "2024-03-12", "2024-03-13"]
    assert r["meta"]["data_through_by_platform"] == {
        "app": "2024-03-13T09:45:00Z", "marketplace": "2024-03-11T15:15:00Z", "website": "2024-03-12T18:05:00Z"}
    assert r["meta"]["data_through"] == "2024-03-13T09:45:00Z"


def test_per_platform_cutoff_single_platform():
    c, _ = client(store=PerPlatformStore())
    r = c.get(URL, params={"start_date": "2024-03-10", "end_date": "2024-03-15", "platforms": "marketplace"}).json()
    assert r["data"]["total"] == [1, 1, None, None, None, None]
    assert r["meta"]["partial_dates"] == ["2024-03-11"]
    assert list(r["meta"]["data_through_by_platform"]) == ["marketplace"]


def test_state_without_per_platform_keys_falls_back_to_global():
    c, _ = client()
    r = c.get(URL, params={"start_date": "2024-03-12", "end_date": "2024-03-13"}).json()
    assert r["data"]["total"] == [3, 3]
    assert set(r["meta"]["data_through_by_platform"].values()) == {"2024-03-13T09:45:00Z"}


# ---------- A2 不完整周期的日均 ----------
def test_month_bucket_daily_avg_and_partial():
    c, _ = client(store=PerPlatformStore())
    r = c.get(URL, params={"start_date": "2024-01-01", "end_date": "2024-03-31", "granularity": "month"}).json()
    d = r["data"]
    assert d["days_in_bucket"] == [31, 29, 31]
    assert d["days_covered"] == [31, 29, 13]  # 3 月：1–13 号，截止当天算一整天
    assert d["partial"] == [False, False, True]
    # FakeStore 每平台每天 1 件：1、2 月日均 3.0；3 月 1–10 号 3 件、11 号 3、12 号 2、13 号 1 → 36 / 13
    assert d["total"][2] == 36 and d["daily_avg"] == [3.0, 3.0, 2.8]


def test_week_bucket_clipped_by_range_is_partial():
    c, _ = client()
    d = c.get(URL, params={"start_date": "2024-01-03", "end_date": "2024-01-15", "granularity": "week",
                           "platforms": "app"}).json()["data"]
    assert d["days_in_bucket"] == [7, 7, 7]
    assert d["days_covered"] == [5, 7, 1]
    assert d["partial"] == [True, False, True]
    assert d["daily_avg"] == [1.0, 1.0, 1.0]


def test_bucket_entirely_after_cutoff_has_null_avg():
    c, _ = client()
    d = c.get(URL, params={"start_date": "2024-03-01", "end_date": "2024-04-30",
                           "granularity": "month"}).json()["data"]
    assert d["days_covered"] == [13, 0] and d["daily_avg"][1] is None and d["total"][1] is None
    assert d["partial"] == [True, True]


def test_day_granularity_has_no_bucket_fields():
    c, _ = client()
    d = c.get(URL, params=OK).json()["data"]
    assert set(d) == {"dates", "series", "total"}


# ---------- A4 源时区未确认 ----------
def test_unconfirmed_source_tz_same_as_request_tz_no_warning():
    c, _ = client()
    m = c.get(URL, params=OK).json()["meta"]  # 默认 tz=UTC，与源时区相同：换算是恒等的
    assert m["source_tz_assumed"] is True and m["warnings"] == []


def test_unconfirmed_source_tz_warns_but_still_answers():
    c, _ = client()
    r = c.get(URL, params={**OK, "tz": "Asia/Shanghai", "platforms": "app,website"})
    assert r.status_code == 200
    (w,) = r.json()["meta"]["warnings"]
    assert w["code"] == "SOURCE_TZ_UNCONFIRMED" and w["message"]
    assert w["platforms"] == ["app", "website"] and w["source_tz"] == {"app": "UTC", "website": "UTC"}


def test_confirmed_source_tz_no_warning():
    s = get_settings()
    c, _ = client(source_tz_confirmed={p: True for p in s.platforms})
    m = c.get(URL, params={**OK, "tz": "Asia/Shanghai"}).json()["meta"]
    assert m["source_tz_assumed"] is False and m["warnings"] == []


def test_utc_aliases_count_as_same_tz():
    c, _ = client()
    assert c.get(URL, params={**OK, "tz": "Etc/UTC"}).json()["meta"]["warnings"] == []


def test_cache_hit_reflects_current_tz_confirmation():
    """只改 confirmed 不重放、不换 data_gen：命中缓存时警告也要按当前配置给。"""
    from salestrend.service import TrendService

    cache = FakeCache()
    c, app = client(cache=cache)
    params = {**OK, "tz": "Asia/Shanghai"}
    assert c.get(URL, params=params).json()["meta"]["warnings"]
    svc: TrendService = app.state.service
    from dataclasses import replace

    svc.s = replace(svc.s, source_tz_confirmed={p: True for p in svc.s.platforms})
    m = c.get(URL, params=params).json()["meta"]
    assert m["cache"] == "hit" and m["warnings"] == [] and m["source_tz_assumed"] is False


def test_cache_key_carries_response_schema_version():
    from salestrend.api.params import RESPONSE_SCHEMA, parse_trend_params

    p = parse_trend_params(class_slug="maxi-dress", rule_version="v1", start_date="2024-01-01",
                           end_date="2024-01-31", platforms=None, tz=None, granularity=None,
                           known_platforms=["app"], max_days=366)
    assert p.cache_key("7").startswith(f"trend:{RESPONSE_SCHEMA}:maxi-dress:v1:7:")
