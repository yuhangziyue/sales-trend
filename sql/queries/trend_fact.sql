-- C 段 · 明细扫描
-- 适用：当天未封口的部分（汇总还没覆盖），或非整点偏移且不在预设列表的时区。
-- 唯一的重查询：API 层单独限流，区间上限 31 天。
SELECT toDate(event_ts, {tz:String}) AS local_date, source, sum(sales_count) AS sales
FROM fact_sales FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND event_ts >= {s:DateTime('UTC')} AND event_ts < {e:DateTime('UTC')}
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
