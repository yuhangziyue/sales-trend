-- 品类 15 分钟表重算：一个（品类, 规则版本）一次，[from, to) 整段先删后写，存绝对值，跑两次结果相同（幂等）。
-- 从商品 15 分钟表汇总而不是从明细算：商品层刚在同一轮重算里建好，再扫一遍明细是重复劳动。
-- 读商品层带 FINAL：重算每轮只付一次，换来品类表一定建在去重后的结果上；查询路径不再碰商品层。
-- 改规则时只重算成员表与这张表（和日表），这正是「汇总按商品存」的回报：商品层与明细都不动。
DELETE FROM agg_class_15m
WHERE class_slug = {cls:String} AND rule_version = {rv:String}
  AND bucket_15m >= {from:DateTime('UTC')} AND bucket_15m < {to:DateTime('UTC')};

INSERT INTO agg_class_15m
SELECT {cls:String}, {rv:String}, source, bucket_15m, sum(sales), {version:UInt64}
FROM agg_product_15m FINAL
WHERE product_id IN (
        SELECT product_id FROM product_class_member FINAL
        WHERE class_slug = {cls:String} AND rule_version = {rv:String} AND decision = 'match')
  AND bucket_15m >= {from:DateTime('UTC')} AND bucket_15m < {to:DateTime('UTC')}
GROUP BY source, bucket_15m;
