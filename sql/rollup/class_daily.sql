-- 品类日表重算：一个（品类, 规则版本, 预设时区）一次，[from, to) 是若干个完整本地日对应的 UTC 区间。
-- 从明细算而不是从小时表算：Asia/Kolkata 这类 +05:30 时区，小时桶会跨本地日界，切不准。
DELETE FROM agg_class_daily
WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND tz = {tz:String}
  AND local_date >= {d1:Date} AND local_date <= {d2:Date};

INSERT INTO agg_class_daily
SELECT {cls:String}, {rv:String}, {tz:String},
       toDate(event_ts, {tz:String}) AS local_date, source, sum(sales_count), {version:UInt64}
FROM fact_sales FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND event_ts >= {from:DateTime('UTC')} AND event_ts < {to:DateTime('UTC')}
GROUP BY local_date, source;
