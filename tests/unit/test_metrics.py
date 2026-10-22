"""/metrics 指标测试。进程内注册表是全局的，一律比较前后差值。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import date

from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.multiprocess import MultiProcessCollector
from test_api import OK, URL, FakeCache, FakeStore, client  # tests/unit 不是包，pytest 把本目录放进 sys.path

from salestrend.cache.resilience import CircuitBreaker
from salestrend.metrics import trend_path
from salestrend.query.planner import Plan, Segment
from salestrend.store import Store


def val(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_exposed_but_not_in_openapi():
    c, app = client()
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "trend_requests_total" in r.text and "trend_request_seconds_bucket" in r.text
    assert "/metrics" not in app.openapi()["paths"]


def test_metrics_not_rate_limited():
    c, _ = client(rate_per_s=0.001, rate_burst=1)
    assert [c.get("/metrics").status_code for _ in range(5)] == [200] * 5


def test_requests_counted_by_cache_and_status():
    c, _ = client(cache=FakeCache())
    before = {k: val("trend_requests_total", cache=k, status="200") for k in ("hit", "miss")}
    bad = val("trend_requests_total", cache="none", status="400")
    c.get(URL, params=OK)
    c.get(URL, params=OK)
    c.get(URL, params={**OK, "tz": "+08:00"})
    assert val("trend_requests_total", cache="miss", status="200") - before["miss"] == 1
    assert val("trend_requests_total", cache="hit", status="200") - before["hit"] == 1
    assert val("trend_requests_total", cache="none", status="400") - bad == 1  # 参数错没走到缓存


def test_storage_failure_counted_as_miss():
    c, _ = client(store=FakeStore(fail=ConnectionError("down")))
    before = val("trend_requests_total", cache="miss", status="503")
    assert c.get(URL, params=OK).status_code == 503
    assert val("trend_requests_total", cache="miss", status="503") - before == 1


def test_latency_histogram_by_path():
    c, _ = client(cache=FakeCache())
    hit0 = val("trend_request_seconds_count", path="cache_hit")
    daily0 = val("trend_request_seconds_count", path="class_daily")
    mixed0 = val("trend_request_seconds_count", path="mixed")
    c.get(URL, params=OK)                                                      # 全部已封口 → 只有日表段
    c.get(URL, params=OK)                                                      # 同 key → 命中
    c.get(URL, params={"start_date": "2024-03-10", "end_date": "2024-03-13"})  # 日表 + 明细
    assert val("trend_request_seconds_count", path="class_daily") - daily0 == 1
    assert val("trend_request_seconds_count", path="cache_hit") - hit0 == 1
    assert val("trend_request_seconds_count", path="mixed") - mixed0 == 1
    # 桶边界覆盖 1ms–5s
    assert REGISTRY.get_sample_value("trend_request_seconds_bucket", {"path": "cache_hit", "le": "0.001"}) is not None
    assert REGISTRY.get_sample_value("trend_request_seconds_bucket", {"path": "cache_hit", "le": "5.0"}) is not None


def test_trend_path_labels():
    assert trend_path({"cache": "hit", "plan": [{"segment": "fact"}]}) == "cache_hit"
    assert trend_path({"cache": "stale", "plan": []}) == "stale"
    assert trend_path({"cache": "miss", "plan": [{"segment": "hourly"}, {"segment": "hourly"}]}) == "hourly"
    assert trend_path({"cache": "miss", "plan": [{"segment": "hourly"}, {"segment": "fact"}]}) == "mixed"
    assert trend_path({"cache": "miss", "plan": []}) == "empty"


def test_storage_queries_counted_per_segment():
    class Client:
        def query(self, sql, parameters):
            return type("R", (), {"result_rows": [(parameters["d1"], "app", 1)]})()

    store = Store(None, client=Client())  # 注入假 client，不连库
    plan = Plan(days=[date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)],
                segments=[Segment("class_daily", [date(2024, 1, 1)]), Segment("hourly", [date(2024, 1, 2)]),
                          Segment("fact", [date(2024, 1, 3)])])
    before = {s: val("storage_queries_total", segment=s) for s in ("class_daily", "hourly", "fact")}
    store.run_plan(plan, "maxi-dress", "v1", "UTC", ["app"])
    assert all(val("storage_queries_total", segment=s) - before[s] == 1 for s in before)


def test_singleflight_shared_counted():
    from salestrend.api.params import parse_trend_params
    from salestrend.service import TrendService
    from salestrend.settings import get_settings

    class SlowStore(FakeStore):
        def run_plan(self, *a):
            import time

            time.sleep(0.1)
            return super().run_plan(*a)

    svc = TrendService(get_settings(), SlowStore(), FakeCache())
    p = parse_trend_params(class_slug="maxi-dress", rule_version="v1", start_date="2024-01-01",
                           end_date="2024-01-31", platforms=None, tz=None, granularity=None,
                           known_platforms=["app", "marketplace", "website"], max_days=366)
    before = val("singleflight_shared_total")

    async def burst():
        await asyncio.gather(*[svc.trend(p) for _ in range(50)])

    asyncio.run(burst())
    assert val("singleflight_shared_total") - before == 49  # 1 个查库，49 个被合并


def test_circuit_open_gauge():
    b = CircuitBreaker(threshold=2, cooldown_s=0.05)
    b.failure()
    b.failure()
    assert val("circuit_open") == 1 and b.is_open
    import time

    time.sleep(0.06)
    assert not b.is_open and val("circuit_open") == 0  # 到期自动关闭，判断时回写


def test_multiprocess_mode_sums_all_workers(tmp_path):
    """模拟 2 个 worker 进程各自计数：MultiProcessCollector 汇总后的总数必须等于两边之和。"""
    script = textwrap.dedent("""
        import sys
        from salestrend.metrics import TREND_REQUESTS, STORAGE_QUERIES
        for _ in range(int(sys.argv[1])):
            TREND_REQUESTS.labels("hit", "200").inc()
        STORAGE_QUERIES.labels("fact").inc()
    """)
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for n in (3, 4):
        subprocess.run([sys.executable, "-c", script, str(n)], env=env, check=True)
    reg = CollectorRegistry()
    MultiProcessCollector(reg, path=str(tmp_path))
    assert reg.get_sample_value("trend_requests_total", {"cache": "hit", "status": "200"}) == 7
    assert reg.get_sample_value("storage_queries_total", {"segment": "fact"}) == 2
