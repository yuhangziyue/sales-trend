-- 对照路径 · 商品 15 分钟表按任意时区切日。规划器不会选它，只供 force 对拍与 P5 对照。
-- 留着的理由：它和品类表读同一层上游，结果必须相等；对拍能在品类表重算漏掉某个版本 / 某段时间时报警。
-- 不在热路径的原因：品类商品分散，按 product_id 打头的排序键跳不过 granule，一年区间等于全表扫描。
SELECT toDate(bucket_15m, {tz:String}) AS local_date, source, sum(sales) AS sales
FROM agg_product_15m FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND bucket_15m >= {s:DateTime('UTC')} AND bucket_15m < {e:DateTime('UTC')}
  AND source IN {platforms:Array(String)}
GROUP BY local_date, source;
