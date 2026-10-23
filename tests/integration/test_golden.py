"""端到端黄金值：真 ClickHouse（make up && make seed），经 API 全链路。数字来自对附件的独立核算。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salestrend.settings import get_settings

pytestmark = pytest.mark.integration
GOLDEN = json.loads((Path(__file__).parents[1] / "golden" / "expected.json").read_text())
URL = "/v1/product-classes/maxi-dress/sales-trend"


def _unavailable(reason: str):
    # 默认失败而不是跳过：跳过会让 make test 在没有数据库时也显示全绿（干净环境审查踩到过）。
    # 只想跑单元测试时显式设 SKIP_INTEGRATION=1，或直接 make test-unit
    if os.getenv("SKIP_INTEGRATION") == "1":
        pytest.skip(reason)
    pytest.fail(f"{reason}. Run `make up && make seed` first (CH_PORT if the host port was changed), "
                "or set SKIP_INTEGRATION=1 to skip integration tests", pytrace=False)


@pytest.fixture(scope="module")
def store():
    from salestrend.store import Store

    try:
        s = Store(get_settings())
        exists = s.client.command("EXISTS TABLE fact_sales")
    except Exception as e:  # noqa: BLE001
        _unavailable(f"ClickHouse not reachable ({type(e).__name__})")
    if not exists:
        _unavailable("tables missing")
    return s


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


def test_rule_v2_without_recomputing_product_layer(api):
    v1 = trend(api, start_date="2024-01-01", end_date="2024-03-31")["meta"]["sum"]["all"]
    v2 = trend(api, start_date="2024-01-01", end_date="2024-03-31", rule_version="v2")["meta"]["sum"]["all"]
    assert v2 - v1 == GOLDEN["elegant_long_dress_sales"]


PATHS = ("class_daily", "class_15m", "product_15m", "fact")


def run_forced(store, force, tz, rv="v1", start=None, end=None):
    from datetime import date

    from salestrend.query.planner import plan_query

    st = store.state()
    p = plan_query(start or date(2024, 1, 1), end or date(2024, 3, 11), tz, sealed_until=st["sealed_until"],
                   data_through=st["data_through"], preset_timezones=get_settings().preset_timezones, force=force)
    return store.run_plan(p, "maxi-dress", rv, tz, ["app", "marketplace", "website"])


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_all_paths_agree(store, tz):
    """对拍：同一请求强制走日表 / 品类 15 分钟表 / 商品 15 分钟表 / 明细四条路径，结果必须一致。"""
    results = [run_forced(store, force, tz) for force in PATHS]
    assert results[0] == results[1] == results[2] == results[3]


@pytest.mark.parametrize("tz", ["Europe/Berlin", "Asia/Kathmandu", "Australia/Eucla"])
def test_class_15m_matches_fact_in_non_preset_tz(store, tz):
    """非预设时区（含 +05:45、+08:45）：品类 15 分钟表切日与扫明细逐日一致。"""
    got = run_forced(store, "class_15m", tz)
    assert got and got == run_forced(store, "fact", tz) == run_forced(store, "product_15m", tz)


@pytest.mark.parametrize("rv", ["v2", "v3"])
def test_other_rule_versions_class_15m_matches_fact(store, rv):
    """每个已加载的规则版本都物化了品类 15 分钟表：v2、v3 的逐日结果与扫明细一致。"""
    got = run_forced(store, "class_15m", "Europe/Berlin", rv)
    assert got and got == run_forced(store, "fact", "Europe/Berlin", rv)


@pytest.mark.parametrize("tz", ["Europe/Berlin", "Asia/Kathmandu"])
def test_non_preset_tz_reads_class_15m(api, store, tz):
    r = trend(api, start_date="2024-01-01", end_date="2024-03-11", tz=tz)
    assert [s["table"] for s in r["meta"]["plan"]] == ["agg_class_15m"]
    assert r["meta"]["sum"]["all"] == sum(run_forced(store, "fact", tz).values())


@pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin", "Asia/Kathmandu"])
def test_today_split_at_seal_matches_fact(api, store, tz):
    """未封口那天按封口线切开：前半天读品类表、尾巴读明细，逐日结果与整段扫明细一致。"""
    from datetime import date

    st = store.state()
    r = trend(api, start_date="2024-03-01", end_date="2024-03-13", tz=tz)
    plan = r["meta"]["plan"]
    assert plan[-1]["table"] == "fact_sales"
    assert plan[-1]["utc_from"] == st["sealed_until"].strftime("%Y-%m-%dT%H:%M:%SZ")
    assert plan[-2]["table"] in ("agg_class_15m", "agg_class_daily") and plan[-2]["to"] == plan[-1]["from"]
    want = run_forced(store, "fact", tz, start=date(2024, 3, 1), end=date(2024, 3, 13))
    got = {s["platform"]: s["values"] for s in r["data"]["series"]}
    for i, d in enumerate(r["data"]["dates"]):
        for src, vals in got.items():
            assert (vals[i] or 0) == want.get((date.fromisoformat(d), src), 0)


def test_concurrent_queries_share_one_client(store):
    """API 用线程池共享一个 client。默认带 session 时同一 session 不许并发，压测里 197/200 报错。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: store.state(), range(200)))
    assert len(results) == 200 and all("sealed_until" in r for r in results)


def test_rollup_is_idempotent(store):
    from salestrend.classify.rules import load_rulesets

    qs = ["SELECT sum(sales), count() FROM agg_product_15m FINAL",
          "SELECT rule_version, sum(sales), count() FROM agg_class_15m FINAL GROUP BY rule_version ORDER BY 1",
          "SELECT rule_version, tz, sum(sales), count() FROM agg_class_daily FINAL GROUP BY 1, 2 ORDER BY 1, 2"]
    before = [store.client.query(q).result_rows for q in qs]
    assert {r[0] for r in before[1]} == {"v1", "v2", "v3"}  # 每个已加载的规则版本都物化
    store.rollup(load_rulesets(get_settings().rules_dir))
    # 不带 FINAL 也要相同：先删后写不留旧行，不靠后台合并兜底
    assert [store.client.query(q).result_rows for q in qs] == before
    raw = "SELECT rule_version, sum(sales), count() FROM agg_class_15m GROUP BY rule_version ORDER BY 1"
    assert store.client.query(raw).result_rows == before[1]
