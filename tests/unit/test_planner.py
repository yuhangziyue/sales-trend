from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from salestrend.query.planner import is_whole_hour_day, local_day_utc, plan_query

PRESET = ("UTC", "Asia/Shanghai", "Asia/Kolkata")
SEALED = datetime(2024, 3, 13, 9, tzinfo=UTC)
THROUGH = datetime(2024, 3, 13, 9, 45, tzinfo=UTC)


def kinds(plan):
    return [(s.segment, s.first.isoformat(), s.last.isoformat()) for s in plan.segments]


def test_preset_tz_history_reads_daily_table_and_today_reads_fact():
    p = plan_query(date(2024, 1, 1), date(2024, 3, 31), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    assert kinds(p) == [("class_daily", "2024-01-01", "2024-03-12"), ("fact", "2024-03-13", "2024-03-13")]
    assert p.partial_dates == {date(2024, 3, 13)}
    assert min(p.null_dates) == date(2024, 3, 14) and max(p.null_dates) == date(2024, 3, 31)
    assert not p.fully_sealed


def test_non_preset_whole_hour_tz_reads_hourly():
    p = plan_query(date(2024, 2, 1), date(2024, 2, 29), "Asia/Tokyo", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    assert kinds(p) == [("hourly", "2024-02-01", "2024-02-29")]
    assert p.fully_sealed


def test_non_preset_half_hour_tz_reads_fact():
    p = plan_query(date(2024, 2, 1), date(2024, 2, 3), "Asia/Kathmandu", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET)
    assert kinds(p) == [("fact", "2024-02-01", "2024-02-03")]


def test_preset_half_hour_tz_still_uses_daily_table():
    p = plan_query(date(2024, 2, 1), date(2024, 2, 3), "Asia/Kolkata", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET)
    assert kinds(p) == [("class_daily", "2024-02-01", "2024-02-03")]


def test_dst_day_is_23_hours_and_still_whole_hour():
    ny = ZoneInfo("America/New_York")
    s, e = local_day_utc(date(2024, 3, 10), ny)
    assert (e - s).total_seconds() == 23 * 3600
    assert is_whole_hour_day(date(2024, 3, 10), ny)


def test_leap_day_and_local_range():
    s, e = local_day_utc(date(2024, 2, 29), ZoneInfo("Asia/Shanghai"))
    assert s == datetime(2024, 2, 28, 16, tzinfo=UTC) and e == datetime(2024, 2, 29, 16, tzinfo=UTC)


def test_everything_after_data_through_is_null():
    p = plan_query(date(2024, 4, 1), date(2024, 4, 3), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    assert p.segments == [] and len(p.null_dates) == 3


def test_force_segment_for_cross_check():
    p = plan_query(date(2024, 1, 1), date(2024, 1, 31), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET, force="fact")
    assert kinds(p) == [("fact", "2024-01-01", "2024-01-31")]


# ---------- 按平台截止 ----------
BY_SRC = {"app": THROUGH, "marketplace": datetime(2024, 3, 11, 15, 15, tzinfo=UTC),
          "website": datetime(2024, 3, 12, 18, 5, tzinfo=UTC)}


def test_platform_cutoffs_null_and_partial():
    p = plan_query(date(2024, 3, 10), date(2024, 3, 15), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET, platform_through=BY_SRC)
    assert p.null_by_platform["marketplace"] == {date(2024, 3, d) for d in range(12, 16)}
    assert p.null_by_platform["website"] == {date(2024, 3, d) for d in range(13, 16)}
    assert p.null_dates == {date(2024, 3, 14), date(2024, 3, 15)}  # 全部平台都没数据才不查库
    # 03-11 marketplace 截止日；03-12 website 截止日且 marketplace 已 null；03-13 app 截止日
    assert p.partial_dates == {date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)}
    assert not p.fully_sealed


def test_platform_cutoffs_use_latest_selected_platform_for_query_range():
    p = plan_query(date(2024, 3, 10), date(2024, 3, 15), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET, platform_through={"marketplace": BY_SRC["marketplace"]})
    assert kinds(p) == [("class_daily", "2024-03-10", "2024-03-11")]  # 只选 marketplace：12 号起不查
    assert p.partial_dates == {date(2024, 3, 11)}


def test_platform_cutoff_follows_report_tz():
    # website 截止 03-12 18:05Z = 上海 03-13 02:05：按上海切日，website 的 03-13 是不完整日而不是 null
    p = plan_query(date(2024, 3, 12), date(2024, 3, 14), "Asia/Shanghai", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET, platform_through=BY_SRC)
    assert p.null_by_platform["website"] == {date(2024, 3, 14)}
    assert date(2024, 3, 13) in p.partial_dates


def test_sealed_history_without_platform_nulls_stays_long_cached():
    p = plan_query(date(2024, 1, 1), date(2024, 1, 31), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET, platform_through=BY_SRC)
    assert p.fully_sealed and not p.partial_dates
