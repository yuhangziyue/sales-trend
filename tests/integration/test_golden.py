"""端到端黄金值：真 ClickHouse（make up && make seed），经 API 全链路。数字来自对附件的独立核算。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salestrend.settings import get_settings

pytestmark = pytest.mark.integration
GOLDEN = json.loads((Path(__file__).parents[1] / "golden" / "expected.json").read_text())
URL = "/v1/product-classes/maxi-dress/sales-trend"


@pytest.fixture(scope="module")
def store():
    from salestrend.store import Store

    try:
        s = Store(get_settings())
        if not s.client.command("EXISTS TABLE fact_sales"):
            pytest.skip("tables missing: run make seed")
        return s
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"ClickHouse not reachable ({type(e).__name__}): run make up && make seed")


@pytest.fixture(scope="module")
def api(store):
    from salestrend.api.app import create_app

    return TestClient(create_app(store=store, use_redis=False))  # 不走 Redis：测的是存储与规划，不是缓存


def trend(api, **params):
    r = api.get(URL, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_fact_and_raw_counts(store):
    assert store.client.command("SELECT count() FROM fact_sales FINAL") == GOLDEN["fact_rows"]
    assert store.client.command("SELECT countIf(is_duplicate = 1) FROM raw_sales_events") >= GOLDEN["duplicates"]


def test_class_members(store):
    rows = store.client.query("SELECT product_id, decision FROM product_class_member FINAL "
                              "WHERE class_slug='maxi-dress' AND rule_version='v1'").result_rows
    d = {pid: dec for pid, dec in rows}
    assert sorted(p for p, x in d.items() if x == "match") == GOLDEN["match_ids"]
    assert d[2027] == "exclude" and d[2017] == "review"


def test_total_and_by_platform(api):
    m = trend(api, start_date="2024-01-01", end_date="2024-03-31")["meta"]
    assert m["sum"] == GOLDEN["sum_utc"]


def test_by_month(api):
    r = trend(api, start_date="2024-01-01", end_date="2024-03-31", granularity="month")
    assert dict(zip(r["data"]["dates"], r["data"]["total"], strict=True)) == GOLDEN["by_month_utc"]


def test_by_month_daily_avg(api):
    """3 月按月合计 188 像「崩了」，日均 14.5 与 1、2 月的 15.6、15.3 基本持平。"""
    d = trend(api, start_date="2024-01-01", end_date="2024-03-31", granularity="month")["data"]
    assert {k: d[k] for k in GOLDEN["by_month_utc_buckets"]} == GOLDEN["by_month_utc_buckets"]


def test_first_three_days(api):
    r = trend(api, start_date="2024-01-01", end_date="2024-01-03")
    got = {s["platform"]: s["values"] for s in r["data"]["series"]}
    assert got == GOLDEN["first_three_days_utc"]


def test_shanghai_february(api):
    """源时区按 UTC 假设（未确认）时：按上海出报表 415，并带 SOURCE_TZ_UNCONFIRMED 警告。
    源时间若本来就是北京时间，结果应是 445，见 test_source_tz.py。"""
    r = trend(api, start_date="2024-02-01", end_date="2024-02-29", tz="Asia/Shanghai")
    assert r["meta"]["sum"]["all"] == GOLDEN["feb_total_shanghai_if_source_utc"]
    assert r["meta"]["source_tz_assumed"] is True
    assert [w["code"] for w in r["meta"]["warnings"]] == ["SOURCE_TZ_UNCONFIRMED"]


def test_utc_report_has_no_tz_warning(api):
    m = trend(api, start_date="2024-02-01", end_date="2024-02-29")["meta"]
    assert m["source_tz_assumed"] is True and m["warnings"] == []


def test_per_platform_cutoff_tail(api):
    """各类合计不变，只有截止较早的平台在截止之后变成 null（原来是 0）。"""
    g = GOLDEN["tail_days_utc"]
    r = trend(api, start_date=g["dates"][0], end_date=g["dates"][-1])
    got = {s["platform"]: s["values"] for s in r["data"]["series"]}
    assert got == {k: g[k] for k in ("app", "marketplace", "website")}
    assert r["data"]["total"] == g["total"]
    assert r["meta"]["partial_dates"] == g["partial_dates"]
    assert r["meta"]["data_through_by_platform"] == GOLDEN["data_through_by_platform"]


def test_after_data_through_is_null(api):
    r = trend(api, start_date="2024-03-14", end_date="2024-03-31")
    assert set(r["data"]["total"]) == {None}


def test_leap_day(api):
    r = trend(api, start_date="2024-02-29", end_date="2024-02-29")
    assert r["data"]["dates"] == ["2024-02-29"] and r["data"]["total"][0] is not None


def test_rule_v2_without_recomputing_hourly(api):
    v1 = trend(api, start_date="2024-01-01", end_date="2024-03-31")["meta"]["sum"]["all"]
    v2 = trend(api, start_date="2024-01-01", end_date="2024-03-31", rule_version="v2")["meta"]["sum"]["all"]
    assert v2 - v1 == GOLDEN["elegant_long_dress_sales"]


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_three_paths_agree(store, tz):
    """对拍：同一请求强制走日表 / 小时表 / 明细三条路径，结果必须一致。"""
    from datetime import date

    from salestrend.query.planner import plan_query

    st = store.state()
    results = []
    for force in ("class_daily", "hourly", "fact"):
        p = plan_query(date(2024, 1, 1), date(2024, 3, 11), tz, sealed_until=st["sealed_until"],
                       data_through=st["data_through"], preset_timezones=get_settings().preset_timezones,
                       force=force)
        results.append(store.run_plan(p, "maxi-dress", "v1", tz, ["app", "marketplace", "website"]))
    assert results[0] == results[1] == results[2]


def test_concurrent_queries_share_one_client(store):
    """API 用线程池共享一个 client。默认带 session 时同一 session 不许并发，压测里 197/200 报错。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: store.state(), range(200)))
    assert len(results) == 200 and all("sealed_until" in r for r in results)


def test_rollup_is_idempotent(store):
    from salestrend.classify.rules import load_rulesets

    q = "SELECT sum(sales), count() FROM agg_product_hourly FINAL"
    before = store.client.query(q).result_rows
    store.rollup(load_rulesets(get_settings().rules_dir))
    assert store.client.query(q).result_rows == before
