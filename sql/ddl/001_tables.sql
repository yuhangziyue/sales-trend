-- 销售趋势服务 · ClickHouse 表结构
-- 写入链路：raw（全部行，审计）→ fact（干净行）→ dim_product → product_class_member（带规则版本）
--          → agg_product_15m（商品×平台×UTC 15 分钟，重算中间层，不在查询路径上）
--          → agg_class_15m（品类×版本×平台×UTC 15 分钟）、agg_class_daily（品类×平台×本地日，仅预设时区）
-- 分区一律按月：ClickHouse 建议分区值在 100–1000 个以内，按天两年就 730 个，容易 Too many parts。

-- ① 原始事件：全部行原样保留（含重复、含拒收），只追加不改。回答「这个数为什么少了 20 件」
CREATE TABLE IF NOT EXISTS raw_sales_events (
  event_key      String,                         -- sha256(七字段规范化值)，hex
  batch_id       String,
  row_no         UInt32,                         -- 源文件行号
  source         LowCardinality(String),
  product_id     Nullable(UInt32),
  title_raw      String,
  sales_count    Nullable(Int32),                -- 有符号：负数异常也能入库被标记
  ts_raw         String,                         -- 原始时间文本，改源时区后可重放
  source_tz      LowCardinality(String),         -- 解析时使用的源时区
  event_ts       Nullable(DateTime64(3, 'UTC')),
  user_id        String,
  category_raw   LowCardinality(String),
  is_duplicate   UInt8,
  dup_of         String,
  dq_flags       Array(LowCardinality(String)),
  ingested_at    DateTime64(3, 'UTC')
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ingested_at)
ORDER BY (batch_id, row_no);

-- ② 干净事实：只进非重复、非拒收行。去重在写入前完成；ReplacingMergeTree 只是兜底
CREATE TABLE IF NOT EXISTS fact_sales (
  event_key   String,
  product_id  UInt32,
  source      LowCardinality(String),
  sales_count UInt32,
  event_ts    DateTime64(3, 'UTC'),
  ingested_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(event_ts)
ORDER BY (event_ts, product_id, event_key);
-- event_ts 打头（v1.2.0 是 product_id 打头）：查询路径上明细只剩「封口线之后的尾巴」，重算也按时间区间扫，
-- 两者都是「全部商品 × 一小段时间」。product_id 打头时品类商品分散、一个 granule 横跨约 50 个商品，
-- 查最后半天也要读整个月分区（P5：85 万行）；时间打头只读 3.2 万行，存储多 2.6%。
-- 去重键随之变成 (event_ts, product_id, event_key)：重复行这三列全同，Replacing 的去重语义不变

-- ③ 商品维表：一个 product_id 一行，标题以 id 为锚
CREATE TABLE IF NOT EXISTS dim_product (
  product_id      UInt32,
  canonical_title String,                        -- 出现次数最多的归一化标题
  title_tokens    Array(String),
  title_variants  Array(String),                 -- 见过的全部原始写法
  category_raw    LowCardinality(String),
  updated_at      DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY product_id;

-- ④ 品类成员：规则引擎输出，多版本并存。改规则只重算这张小表和两张品类表，不动事实和商品层
CREATE TABLE IF NOT EXISTS product_class_member (
  class_slug    LowCardinality(String),
  rule_version  LowCardinality(String),
  product_id    UInt32,
  decision      Enum8('match' = 1, 'exclude' = 2, 'review' = 3),
  matched_rule  LowCardinality(String),
  warnings      Array(LowCardinality(String)),
  classified_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(classified_at)
ORDER BY (class_slug, rule_version, product_id);

-- ⑤ 商品 15 分钟汇总：按商品存（不按品类），存绝对值，重算整段覆盖 → 迟到、去重修正、改规则都幂等。
--    15 分钟而不是 1 小时：现行全部时区的偏移都是 15 分钟的整数倍，这一粒度能切准任何时区的日界。
--    只当重算中间层：稀疏数据下它的行数接近明细（P5：986 万 / 1000 万，小时粒度也有 945 万），品类商品又分散，
--    按 product_id 打头的排序键一个 granule 也跳不过，查询一年等于全表扫描，所以不直接服务查询
CREATE TABLE IF NOT EXISTS agg_product_15m (
  product_id  UInt32,
  source      LowCardinality(String),
  bucket_15m  DateTime('UTC'),
  sales       UInt64,
  events      UInt32,
  version     UInt64                             -- 重算批次号
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(bucket_15m)
ORDER BY (product_id, bucket_15m, source);

-- ⑥ 品类日汇总：只为「品类 × 规则版本 × 预设时区」物化，且只物化已封口的本地日
CREATE TABLE IF NOT EXISTS agg_class_daily (
  class_slug   LowCardinality(String),
  rule_version LowCardinality(String),
  tz           LowCardinality(String),
  local_date   Date,
  source       LowCardinality(String),
  sales        UInt64,
  version      UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(local_date)
ORDER BY (class_slug, rule_version, tz, local_date, source);

-- ⑥b 品类 15 分钟汇总：非预设时区与「未封口那天的前半天」的热路径。从 ⑤ 汇总、按成员表的 match 过滤，
--    每个已加载的规则版本都物化：一个版本一年至多 3 平台 × 35,136 格 ≈ 10.5 万行，查询只读十几个 granule。
--    改规则时只重算成员表和这张小表（连同日表），商品层与明细都不动
CREATE TABLE IF NOT EXISTS agg_class_15m (
  class_slug   LowCardinality(String),
  rule_version LowCardinality(String),
  source       LowCardinality(String),
  bucket_15m   DateTime('UTC'),
  sales        UInt64,
  version      UInt64                            -- 重算批次号
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(bucket_15m)
ORDER BY (class_slug, rule_version, bucket_15m, source);

-- ⑦ 管线水位：sealed_until（汇总封口到哪个 UTC 15 分钟格）、data_through（各平台数据最新时间）
CREATE TABLE IF NOT EXISTS pipeline_state (
  key        String,
  value      DateTime64(3, 'UTC'),
  updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY key;
