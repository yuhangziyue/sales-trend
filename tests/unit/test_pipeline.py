from collections import Counter
from datetime import UTC, datetime

from salestrend.ingest.pipeline import clean, process


def test_dedupe_counts(events):
    assert len(events) == 300
    assert sum(e.is_duplicate for e in events) == 12
    assert len(clean(events)) == 288


def test_duplicates_point_to_an_earlier_identical_row(events):
    first = {}
    for e in events:
        if e.is_duplicate:
            assert e.event_key in first and first[e.event_key] < e.row_no
        else:
            first.setdefault(e.event_key, e.row_no)


def test_maxi_dress_baseline(events):
    good = [e for e in clean(events) if 2001 <= e.product_id <= 2012]
    by_src = Counter()
    for e in good:
        by_src[e.source] += e.sales_count
    assert dict(by_src) == {"app": 368, "marketplace": 372, "website": 377}
    assert len(good) == 119


def test_cross_batch_dedupe(rows, settings, events):
    again = process(rows, settings.platforms, known_keys={e.event_key for e in events})
    assert clean(again) == []
    assert all(e.is_duplicate for e in again)


def test_title_variants_are_not_duplicates(settings):
    base = {"product_id": 2003, "sales_count": 5, "timestamp": "2024-01-05 10:00:00",
            "user_id": "U1", "category": "dress", "source": "app"}
    ev = process([{**base, "title": "Red Maxi Dress"}, {**base, "title": "Red Maxi-Dress"}], settings.platforms)
    assert len(clean(ev)) == 2  # 不同写法是两次上报，不能被去重吃掉


def test_reject_rules(settings):
    base = {"product_id": 2001, "title": "Maxi Dress", "sales_count": 3, "timestamp": "2024-01-05 10:00:00",
            "user_id": "U1", "category": "dress", "source": "app"}
    cases = {
        "bad_product_id": {"product_id": "abc"},
        "bad_timestamp": {"timestamp": "2024-13-45 10:00"},
        "negative_sales": {"sales_count": -2},
        "unknown_source": {"source": "tiktok"},
        "bad_sales_count": {"sales_count": None},
    }
    for flag, patch in cases.items():
        e = process([{**base, **patch}], settings.platforms)[0]
        assert e.rejected and flag in e.dq_flags, flag


def test_warn_rules(settings):
    rows = [{"product_id": 2001, "title": "Maxi Dress", "sales_count": 2, "timestamp": f"2024-01-{d:02d} 10:00:00",
             "user_id": f"U{d}", "category": "dress", "source": "app"} for d in range(1, 21)]
    rows.append({**rows[0], "user_id": "BIG", "sales_count": 500})
    rows.append({**rows[0], "user_id": "FUT", "timestamp": "2030-01-01 10:00:00"})
    rows.append({**rows[0], "user_id": "EDGE", "timestamp": "2024-01-31 23:55:00"})
    ev = {e.user_id: e for e in process(rows, settings.platforms, now=datetime(2026, 1, 1, tzinfo=UTC))}
    assert "sales_outlier" in ev["BIG"].dq_flags and not ev["BIG"].rejected
    assert "future_ts" in ev["FUT"].dq_flags
    assert "day_boundary" in ev["EDGE"].dq_flags


def test_source_timezone_is_applied(rows):
    sh = process(rows, {"app": "Asia/Shanghai", "marketplace": "UTC", "website": "UTC"})
    e = next(x for x in sh if x.user_id == "U2092")  # app 2024-02-01 00:05 本地
    assert e.event_ts == datetime(2024, 1, 31, 16, 5, tzinfo=UTC)
    assert e.ts_raw == "2024-02-01 00:05:00"  # 原文保留，可重放
