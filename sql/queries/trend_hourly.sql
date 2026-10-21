-- B 段 · 小时表按任意整点时区切日
-- 适用：tz 不在预设列表但是整点偏移（含夏令时整点切换），且时间已封口。
-- 品类在查询时由成员表决定：规则改版不用重算这张表。
SELECT toDate(bucket_hour, {tz:String}) AS local_date, source, sum(sales) AS sales
FROM agg_product_hourly FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND bucket_hour >= {s:DateTime('UTC')} AND bucket_hour < {e:DateTime('UTC')}   -- UTC 半开区间
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
