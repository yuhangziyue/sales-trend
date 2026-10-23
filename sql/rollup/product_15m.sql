-- 商品 15 分钟汇总重算：[from, to) 整段先删后写，存绝对值。跑两次结果相同（幂等）。
-- 只写 version 而不靠 Replacing 合并去覆盖：某个格子的销量被去重修正成 0 时，旧行必须消失，
-- 单靠 ReplacingMergeTree 覆盖做不到（新结果里根本没有这一行），所以先删。
DELETE FROM agg_product_15m WHERE bucket_15m >= {from:DateTime('UTC')} AND bucket_15m < {to:DateTime('UTC')};

INSERT INTO agg_product_15m
SELECT product_id, source, toStartOfFifteenMinutes(event_ts) AS bucket_15m,
       sum(sales_count), count(), {version:UInt64}
FROM fact_sales FINAL
WHERE event_ts >= {from:DateTime('UTC')} AND event_ts < {to:DateTime('UTC')}
GROUP BY product_id, source, bucket_15m;
