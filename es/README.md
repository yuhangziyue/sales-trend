# 沿用 Elasticsearch 时的等价实现（对照，不运行）

出题方现有系统用 ES。本目录给出「如果必须沿用 ES」的等价 mapping 与查询，说明代价在哪，以及为什么主方案选了 ClickHouse。

| 文件 | 内容 |
|---|---|
| [mapping.json](mapping.json) | `PUT sales_events` 的 settings 与 mappings |
| [query.json](query.json) | 按日、按平台统计销量的聚合查询 |

## 怎么映射

| 本方案的概念 | ES 的做法 |
|---|---|
| 写前去重 | `_id = event_key`，同一事件重复写入是覆盖写，天然幂等 |
| 品类归属 | 写入时由规则引擎算好，打进文档的 `class_slugs` 与 `rule_version` 字段 |
| 任意时区切日 | `date_histogram` 的 `time_zone` 参数，`range` 查询同样带 `time_zone` |
| 补零 | `min_doc_count: 0` 加 `extended_bounds`；null 与 0 的区分仍需应用层按数据截止时间处理 |
| 缓存 | shard request cache（仅 `size: 0` 生效） |

## 代价（这也是不选 ES 做主存储的原因）

1. **规则改版要重刷全量文档**：品类归属写死在文档里，改规则就要 `update_by_query` 全量重刷。本方案按商品存汇总、查询时 join 成员表，改规则只重算一张小表。
2. **没有好走的预聚合路线**：rollup 已废弃，downsampling 只适用于时序数据流（TSDS），剩下的 transforms 只能做固定维度的 pivot，任意时区切日仍然得扫明细。
3. **request cache 在高写入下命中率低**：每次 refresh 都会让缓存失效。持续采集场景下，缓存基本靠应用层（Redis）兜。
4. **聚合吃堆内存**：长区间 × 多平台的 `date_histogram` 在大数据量下需要控制 bucket 数与分片数。

保留 ES 的合理位置：**标题全文检索**（运营搜「碎花长裙」找商品）。这正是 ES 的主场，与趋势统计分开。
