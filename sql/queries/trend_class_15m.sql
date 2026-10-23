-- B 段 · 品类 15 分钟表按任意时区切日
-- 适用：tz 不在预设列表的已封口日（含 +05:45 这类非整点时区），以及未封口那天里 sealed_until 之前的部分。
-- [s, e) 是 UTC 半开区间，可以截在一天中间；15 分钟格能对齐任何现行时区的日界。
-- 排序键 (class_slug, rule_version, bucket_15m) 打头：一个品类一年至多读 10.5 万行，和明细量无关。
SELECT toDate(bucket_15m, {tz:String}) AS local_date, source, sum(sales) AS sales
FROM agg_class_15m FINAL
WHERE class_slug = {cls:String} AND rule_version = {rv:String}
  AND bucket_15m >= {s:DateTime('UTC')} AND bucket_15m < {e:DateTime('UTC')}
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
