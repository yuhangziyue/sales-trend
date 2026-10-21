"""源时区假设的端到端证明：真实 xlsx → 写入管线 → 规划器 → 组装 → API，存储换成内存实现。

不改全局配置、不动 ClickHouse：用 dataclasses.replace 造一份「三个平台源时区 = Asia/Shanghai 且已确认」的
Settings，同一份 xlsx 按它重新解析。要证明的是：

- 源时间按 UTC 假设，请求 tz=Asia/Shanghai：2 月 415（每条事件被再推后 8 小时，跨月探针换月）
- 源时间本来就是北京时间，请求 tz=Asia/Shanghai：2 月 445，与「按时间戳上写的日期原样切日」逐日相同
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from salestrend.api.app import create_app
from salestrend.ingest.pipeline import clean, process

GOLDEN = json.loads((Path(__file__).parents[1] / "golden" / "expected.json").read_text())
URL = "/v1/product-classes/maxi-dress/sales-trend"
FEB = {"start_date": "2024-02-01", "end_date": "2024-02-29"}


class MemoryStore:
    """接口与 Store 的 state / run_plan 相同；三段规划都按明细算，品类成员取黄金值。"""

    def __init__(self, events):
        self.events = [e for e in clean(events) if e.product_id in GOLDEN["match_ids"]]

    def state(self):
        through = max(e.event_ts for e in self.events)
        st = {"sealed_until": through.replace(minute=0, second=0, microsecond=0), "data_through": through}
        for e in self.events:
            st[f"data_through:{e.source}"] = max(st.get(f"data_through:{e.source}", e.event_ts), e.event_ts)
        return st

    def run_plan(self, plan, cls, rv, tz_name, platforms):
        tz, out = ZoneInfo(tz_name), defaultdict(int)
        wanted = {d for seg in plan.segments for d in seg.dates}
        for e in self.events:
            d = e.event_ts.astimezone(tz).date()
            if e.source in platforms and d in wanted:
                out[(d, e.source)] += e.sales_count
        return out


def api_for(settings, source_tz: str, confirmed: bool, rows):
    s = replace(settings, platforms={p: source_tz for p in settings.platforms},
                source_tz_confirmed={p: confirmed for p in settings.platforms})
    return TestClient(create_app(settings=s, store=MemoryStore(process(rows, s.platforms)), use_redis=False))


def get(api, **params):
    r = api.get(URL, params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def as_utc(settings, rows):
    return api_for(settings, "UTC", False, rows)


@pytest.fixture(scope="module")
def as_shanghai(settings, rows):
    return api_for(settings, "Asia/Shanghai", True, rows)


def test_memory_store_matches_golden(as_utc):
    assert get(as_utc, start_date="2024-01-01", end_date="2024-03-31")["meta"]["sum"] == GOLDEN["sum_utc"]


def test_source_utc_report_shanghai_is_415_with_warning(as_utc):
    m = get(as_utc, **FEB, tz="Asia/Shanghai")["meta"]
    assert m["sum"]["all"] == GOLDEN["feb_total_shanghai_if_source_utc"]
    assert [w["code"] for w in m["warnings"]] == ["SOURCE_TZ_UNCONFIRMED"]


def test_source_shanghai_report_shanghai_is_445(as_shanghai):
    m = get(as_shanghai, **FEB, tz="Asia/Shanghai")["meta"]
    assert m["sum"]["all"] == GOLDEN["feb_total_shanghai_if_source_shanghai"] == GOLDEN["by_month_utc"]["2024-02-01"]
    assert m["source_tz_assumed"] is False and m["warnings"] == []


def test_same_source_and_report_tz_cuts_days_as_written(as_utc, as_shanghai):
    """源时区 = 报表时区时，换算是恒等的：逐日结果与「UTC 入库、UTC 出报表」完全一致。"""
    a = get(as_utc, start_date="2024-01-01", end_date="2024-03-10")["data"]
    b = get(as_shanghai, start_date="2024-01-01", end_date="2024-03-10", tz="Asia/Shanghai")["data"]
    assert a["series"] == b["series"] and a["total"] == b["total"]


def test_confirmed_source_tz_converts_without_warning(as_shanghai):
    """源时区已确认时，按别的时区出报表是正常换算：结果随之变化，但不再告警。"""
    m = get(as_shanghai, **FEB)["meta"]  # 源是北京时间、按 UTC 出报表，事件提前 8 小时
    assert m["sum"]["all"] != GOLDEN["by_month_utc"]["2024-02-01"]
    assert m["source_tz_assumed"] is False and m["warnings"] == []
