-- A 段 · 日表直读
-- 适用：tz 在预设列表，且本地日完整落在 sealed_until 之前。绝大多数历史查询走这里，每天每平台一行。
SELECT local_date, source, sum(sales) AS sales
FROM agg_class_daily FINAL
WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND tz = {tz:String}
  AND local_date >= {d1:Date} AND local_date <= {d2:Date}
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
