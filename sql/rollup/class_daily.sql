-- 品类日表重算：一个（品类, 规则版本, 预设时区）一次，[from, to) 是若干个完整本地日对应的 UTC 区间。
-- 从明细算。最初的理由是小时桶切不准 Asia/Kolkata（+05:30）的日界；改成 15 分钟粒度后这个理由已不成立，
-- 从 agg_product_15m 汇总也能切准任意时区、重算更省。保留从明细算，是让日表与 15 分钟层各自独立地从明细得出，
-- 对拍时能互相校验；改为从 15 分钟层汇总列在路线图。
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
