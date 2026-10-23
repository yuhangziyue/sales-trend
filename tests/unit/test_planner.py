from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from salestrend.query.planner import floor_bucket, local_day_utc, on_grid, plan_query

PRESET = ("UTC", "Asia/Shanghai", "Asia/Kolkata")
SEALED = datetime(2024, 3, 13, 9, tzinfo=UTC)
THROUGH = datetime(2024, 3, 13, 9, 45, tzinfo=UTC)


def kinds(plan):
    return [(s.segment, s.first.isoformat(), s.last.isoformat()) for s in plan.segments]


def utc(plan):
    return [(s.segment, s.describe()["utc_from"], s.describe()["utc_to"]) for s in plan.segments]


def test_preset_tz_history_reads_daily_table_and_today_splits_at_seal():
    p = plan_query(date(2024, 1, 1), date(2024, 3, 31), "UTC", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    # 未封口那天按封口线切开：前半天读品类 15 分钟表，只有 09:00 之后的尾巴读明细
    assert utc(p) == [("class_daily", "2024-01-01T00:00:00Z", "2024-03-13T00:00:00Z"),
                      ("class_15m", "2024-03-13T00:00:00Z", "2024-03-13T09:00:00Z"),
                      ("fact", "2024-03-13T09:00:00Z", "2024-03-14T00:00:00Z")]
    assert kinds(p)[1:] == [("class_15m", "2024-03-13", "2024-03-13"), ("fact", "2024-03-13", "2024-03-13")]
    assert p.partial_dates == {date(2024, 3, 13)}
    assert min(p.null_dates) == date(2024, 3, 14) and max(p.null_dates) == date(2024, 3, 31)
    assert not p.fully_sealed


def test_non_preset_tz_reads_class_15m():
    p = plan_query(date(2024, 2, 1), date(2024, 2, 29), "Asia/Tokyo", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    assert kinds(p) == [("class_15m", "2024-02-01", "2024-02-29")]
    assert p.fully_sealed
    # 品类层读 agg_class_15m；商品层只是重算中间层，不在查询路径上
    assert p.segments[0].describe()["table"] == "agg_class_15m"


def test_non_preset_tz_today_merges_sealed_part_into_one_query():
    # Berlin 的 03-13 从 03-12 23:00Z 起算：已封口的历史日与当天前半天首尾相接，合成一段查询
    p = plan_query(date(2024, 3, 1), date(2024, 3, 13), "Europe/Berlin", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET)
    assert utc(p) == [("class_15m", "2024-02-29T23:00:00Z", "2024-03-13T09:00:00Z"),
                      ("fact", "2024-03-13T09:00:00Z", "2024-03-13T23:00:00Z")]
    assert kinds(p) == [("class_15m", "2024-03-01", "2024-03-13"), ("fact", "2024-03-13", "2024-03-13")]
    assert [s.describe()["table"] for s in p.segments] == ["agg_class_15m", "fact_sales"]


def test_non_preset_quarter_hour_tz_reads_class_15m_not_fact():
    # +05:45：日界在 18:15Z，落在 15 分钟格上，已封口的日子不再扫明细
    p = plan_query(date(2024, 2, 1), date(2024, 2, 3), "Asia/Kathmandu", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET)
    assert utc(p) == [("class_15m", "2024-01-31T18:15:00Z", "2024-02-03T18:15:00Z")]


def test_quarter_hour_tz_today_splits_at_seal():
    # Kathmandu 的 03-13 = 03-12 18:15Z .. 03-13 18:15Z；03-14 本地日已晚于数据截止
    p = plan_query(date(2024, 3, 12), date(2024, 3, 14), "Asia/Kathmandu", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET)
    assert utc(p) == [("class_15m", "2024-03-11T18:15:00Z", "2024-03-13T09:00:00Z"),
                      ("fact", "2024-03-13T09:00:00Z", "2024-03-13T18:15:00Z")]
    assert p.null_dates == {date(2024, 3, 14)}


def test_preset_half_hour_tz_still_uses_daily_table():
    p = plan_query(date(2024, 2, 1), date(2024, 2, 3), "Asia/Kolkata", sealed_until=SEALED,
                   data_through=THROUGH, preset_timezones=PRESET)
    assert kinds(p) == [("class_daily", "2024-02-01", "2024-02-03")]


def test_off_grid_day_boundary_falls_back_to_fact():
    # 1971 年 Africa/Monrovia 是 −0:44:30，日界不在 15 分钟格上：只能扫明细
    d = date(1971, 6, 1)
    assert not on_grid(local_day_utc(d, ZoneInfo("Africa/Monrovia"))[0])
    p = plan_query(d, d, "Africa/Monrovia", sealed_until=SEALED, data_through=THROUGH, preset_timezones=PRESET)
    assert kinds(p) == [("fact", "1971-06-01", "1971-06-01")]


def test_dst_day_is_23_hours_and_split_on_grid():
    ny = ZoneInfo("America/New_York")
    s, e = local_day_utc(date(2024, 3, 10), ny)
    assert (e - s).total_seconds() == 23 * 3600
    # 夏令时切换日当天未封口：日界与封口线都按 UTC 算，切在 23 小时的这一天中间
    sealed = datetime(2024, 3, 10, 15, 30, tzinfo=UTC)
    p = plan_query(date(2024, 3, 9), date(2024, 3, 10), "America/New_York", sealed_until=sealed,
                   data_through=datetime(2024, 3, 10, 15, 40, tzinfo=UTC), preset_timezones=("America/New_York",))
    assert utc(p) == [("class_daily", "2024-03-09T05:00:00Z", "2024-03-10T05:00:00Z"),
                      ("class_15m", "2024-03-10T05:00:00Z", "2024-03-10T15:30:00Z"),
                      ("fact", "2024-03-10T15:30:00Z", "2024-03-11T04:00:00Z")]


def test_fall_back_day_25_hours_on_non_preset_tz():
    p = plan_query(date(2024, 10, 27), date(2024, 10, 27), "Europe/Berlin", sealed_until=SEALED.replace(month=12),
                   data_through=THROUGH.replace(month=12), preset_timezones=PRESET)
    assert utc(p) == [("class_15m", "2024-10-26T22:00:00Z", "2024-10-27T23:00:00Z")]  # 25 小时


def test_seal_not_yet_reached_today_reads_whole_day_from_fact():
    p = plan_query(date(2024, 3, 13), date(2024, 3, 13), "UTC", sealed_until=datetime(2024, 3, 12, 23, tzinfo=UTC),
                   data_through=THROUGH, preset_timezones=PRESET)
    assert utc(p) == [("fact", "2024-03-13T00:00:00Z", "2024-03-14T00:00:00Z")]


def test_floor_bucket():
    assert floor_bucket(datetime(2024, 3, 13, 9, 44, 59, 9, tzinfo=UTC)) == datetime(2024, 3, 13, 9, 30, tzinfo=UTC)


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


def test_force_product_15m_is_cross_check_only():
    p = plan_query(date(2024, 1, 1), date(2024, 1, 31), "Asia/Tokyo", sealed_until=SEALED, data_through=THROUGH,
                   preset_timezones=PRESET, force="product_15m")
    assert kinds(p) == [("product_15m", "2024-01-01", "2024-01-31")]
    assert p.segments[0].describe()["table"] == "agg_product_15m"


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
