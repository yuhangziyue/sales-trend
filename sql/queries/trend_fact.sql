-- C 段 · 明细扫描
-- 适用：sealed_until 之后的尾巴（至多 15 分钟 + 一个重算间隔，重算停摆时会变长），
-- 以及日界不落在 15 分钟格上的零头（只有 1972 年前的地方平时，现行时区没有）。
-- [s, e) 是 UTC 半开区间，可以从一天中间开始。
SELECT toDate(event_ts, {tz:String}) AS local_date, source, sum(sales_count) AS sales
FROM fact_sales FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND event_ts >= {s:DateTime('UTC')} AND event_ts < {e:DateTime('UTC')}
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
