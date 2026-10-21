# 已有 Elasticsearch 时怎么落地（对照，不运行）

主方案按 ClickHouse 设计。题面写的是「可**继续**使用 Elasticsearch」，说明现有系统用 ES。本目录回答三个问题：两者怎么分工；已有 ES 时，按最佳实践改核心和只改查询落地层分别怎么做；什么条件下才值得引入 ClickHouse。

| 文件 | 内容 |
|---|---|
| [mapping.json](mapping.json) | 明细索引 `sales_events` 的 settings 与 mappings |
| [query.json](query.json) | 直接扫明细，按日、按平台统计（最朴素的写法，规模小时够用） |
| [transform.json](transform.json) | 连续 transform：明细 → 商品 × 平台 × UTC 小时汇总索引 |
| [query-hourly-lookup.json](query-hourly-lookup.json) | 读小时汇总，用 terms lookup 引用带版本的品类成员文档 |

## 1 · 分工：按场景，不二选一

| 场景 | 归谁 | 理由 |
|---|---|---|
| 商品检索、标题搜索、下钻看商品列表 | ES | 倒排索引、分析链、相关度排序是它的主场 |
| 标题归一化的**候选召回** | ES | 分析链本身就是归一化：`lowercase`、`asciifolding`、`word_delimiter_graph`（拆 Maxi-Dress）、`synonym_graph`（近义词，可热更新）、`dictionary_decompounder`（拆 Maxikleid）、IK / ICU（中文分词） |
| 品类**判定**（中心词、长度分档、用途排除） | 规则引擎（[v3](../docs/classification-v3.md)） | 查询 DSL 难以表达，也难以审计。判定结果写成带版本的成员表，作为两边共用的 key |
| 销量明细、汇总、趋势查询 | ClickHouse + Redis | 只追加的事实数据，需要精确聚合 |

两边靠 `product_id` 关联。趋势结果**不写回 ES**：多一份副本，就多一处对不上时说不清以谁为准。唯一的例外是前端只对接 ES、改接口成本太高；这时把日粒度汇总（数据量很小）定时写入 ES 的一个索引，作为对外提供数据的一层，并写明这是向现实让步的取舍。

## 2 · 已有 ES：两条改造路线

### 路线 A：只改查询落地层（改动最小，推荐先走）

ES 继续保存商品和明细，不动核心。新增 ClickHouse 只承接销量：

```
采集 ──► 写入管线（去重、event_key） ─┬─► ES sales_events   （原有，保留检索能力）
                                     └─► ClickHouse fact  （新增，承接聚合）
API 层：趋势查询改走 ClickHouse + Redis；商品检索仍走 ES
```

- 两边写入在同一条管线里，由同一个 `event_key` 保证幂等，不做跨库双写事务。
- 先影子运行：同一请求两边各查一次，对拍一致后再切流量，ES 侧的聚合查询保留为回退通道。
- 代价：多运维一套 ClickHouse；写入多一个出口。

### 路线 B：按最佳实践改造 ES 核心（不引入新存储）

把主方案的同一套架构落在 ES 上：

| 主方案（ClickHouse） | ES 上的做法 | 文件 |
|---|---|---|
| 写前去重 | `_id = event_key`，重复写入即覆盖，精确且立即生效。这一点 ES 比 ClickHouse 强 | mapping.json |
| 商品 × 平台 × UTC 小时汇总 | 连续 transform；`sync` 字段用**入库时间** `ingest_ts`，迟到数据才会被重算进对应小时 | transform.json |
| 带版本的品类成员表，改规则不重算 | terms lookup 引用成员文档 `class_members/maxi-dress@v1`；改规则只重写这一个文档。默认上限 65536 个 id（`index.max_terms_count`） | query-hourly-lookup.json |
| 任意整点时区切日 | 对小时汇总做 `date_histogram`，带 `time_zone` | query-hourly-lookup.json |
| 按月分区、冷热分层 | data stream + ILM：按 30 天或单主分片 50GB 滚动；hot → warm（`forcemerge` 到 1 段）→ cold；单分片控制在 10–50GB | — |
| 缓存 | shard request cache 只对 `size: 0` 生效，每次 refresh 都会失效。汇总索引写入频率低，命中率高；持续写入的明细索引要调大 `refresh_interval`（如 30s），主要靠 Redis | — |

路线 B 的上限也要写清楚：
- terms 聚合跨分片是近似的（会返回误差上界），cardinality 是 HyperLogLog 近似，**对账和 KPI 结算这类必须精确的数字不能依赖它们**。按日 sum 的 date_histogram 是精确的。
- 没有 SQL join 和窗口函数，分析师不能自助做即席分析。
- 同样的数据，索引体积通常比列存大好几倍（以 POC 实测为准）。
- 半点时区（如 Asia/Kolkata）同样切不准小时汇总，要么扫明细，要么另建一个按该时区切日的 transform。

## 3 · 什么时候才值得引入 ClickHouse：按场景，看阈值

引入新存储的理由，只能是「它更适合做这件事，而且规模已经越过了现有方案能撑住的线」。光说「更适合」不够，下表给出触发线。**数字是经验值，必须用真实数据做 POC 实测后再定**：拿三个月真实明细，同时灌进现有 ES 和 ClickHouse，跑同一组查询，比较 P99、存储量和资源占用。

| 维度 | 留在 ES（路线 B 足够） | 触发引入 ClickHouse |
|---|---|---|
| 明细规模 | 总量在亿级以下，日增百万级 | 总量十亿级以上，或日增千万级 |
| 查询形态 | 固定维度看板，transform 能覆盖 | 维度组合不固定、要即席分析，分析师要自己写 SQL |
| 精确性 | 趋势参考，允许近似 | 对账、KPI 结算、对外报告，要求精确 |
| 分组基数 | 按平台、品类这类低基数分组 | 百万级商品的 Top N、按商品排行 |
| 留存周期 | 热数据几个月 | 多年留存，冷数据要压缩到对象存储 |
| 写入模式 | 按文档频繁更新（库存、价格、状态） | 只追加的事件流 |
| 团队 | 只熟 ES，没有 ClickHouse 运维经验 | 有人能负责 ClickHouse 的运维、备份和升级 |

判断规则：**命中一条「硬触发」**（必须精确的结算，或者需要即席分析），**或者命中两条以上规模类触发**，再引入；否则先走路线 B，把现有 ES 用到位。

面试时应该反问的问题，答案决定走哪条路线：现在 ES 里的明细有多少、日增多少；哪些聚合查询慢、慢在哪；这份趋势数据是否用于结算；团队在 ES 上已经积累了哪些实践（ILM、transform、索引模板）；有没有人能运维 ClickHouse。
