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
