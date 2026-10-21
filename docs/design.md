# 系统与数据设计说明

> 每个选型按「为什么 / 反方 / 何时换」写。代码位置用相对路径标出，方便对照。

## 0 · 一句话

写入时把脏活干完（时区、去重、质量标记），汇总存到「商品 × 平台 × UTC 小时」，查询时由规划器把日期区间拆成「日表 / 小时表 / 明细」三段，每段用最便宜的表，再叠一层 Redis。

架构图见 [architecture.mmd](architecture.mmd)（README 内嵌渲染版）。

---

## 1 · 存储选型

| 维度 | ✅ ClickHouse | PostgreSQL | Elasticsearch | DuckDB |
|---|---|---|---|---|
| 精确去重 | 最终一致，需写前去重 | 唯一约束，强 | `_id` 覆盖写 | 强 |
| 维表 join | 支持；物化视图只由左表触发 | 完整 | 基本不行，写时打宽 | 完整 |
| 规则改版重算 | 按分区 / 区间替换 | 按分区重灌 | 全量重刷文档，最贵 | 全量重算 |
| 任意时区切日 | 函数带时区参数 | 明细可以 | `time_zone` 参数 | 可以 |
| 高 QPS 小查询 | 查询要轻 | 汇总表 + B-tree，强 | 靠 request cache，refresh 即失效 | 不适合 |
| 海量明细聚合 | 主场 | 行存，日增千万吃力 | 可以，耗堆内存 | 单机强 |
| 运维成本 | 中 | 低 | 中高 | 零 |

- **为什么 ClickHouse**：题面设定「持续多源采集、大数据量、高并发」，列存聚合是主场。它的两个短板都有绕法：去重前移到写入前；品类维表很小，join 代价可忽略。
- **反方 PostgreSQL**：日增百万以内、或团队只熟 PG 时，按月分区 + BRIN + 汇总表完全够用，运维更省。PG 的短板是行存扫大明细，这个没有绕法。**这是正式备选，不是陪衬。**
- **反方 ES**：rollup 已废弃，downsampling 只适用于时序数据流；品类写在文档里，规则一改就得全量重刷。等价 mapping 与查询见 [`es/`](../es/README.md)。ES 适合留着做标题全文检索。
- **何时换**：日增量 < 100 万且无多源实时需求 → PG；需要全文检索 → 加 ES 做检索，不做统计。

## 2 · 表结构

DDL：[`sql/ddl/001_tables.sql`](../sql/ddl/001_tables.sql)。

| 表 | 引擎 | 排序键 | 分区 | 作用 |
|---|---|---|---|---|
| raw_sales_events | MergeTree | batch_id, row_no | 入库月 | 全部原始行，含重复与拒收，审计用 |
| fact_sales | ReplacingMergeTree | product_id, event_ts, event_key | 事件月 | 干净行；Replacing 只是兜底 |
| dim_product | ReplacingMergeTree | product_id | — | 以 id 为锚，记录全部标题写法 |
| product_class_member | ReplacingMergeTree | class_slug, rule_version, product_id | — | 规则引擎输出，多版本并存 |
| agg_product_hourly | ReplacingMergeTree | product_id, bucket_hour, source | 月 | 商品 × 平台 × UTC 小时 |
| agg_class_daily | ReplacingMergeTree | class_slug, rule_version, tz, local_date, source | 月 | 预设时区的品类日表 |
| pipeline_state | ReplacingMergeTree | key | — | 封口水位与数据截止时间 |

三个关键决定：

| 决定 | 选择 | 为什么 | 反方 |
|---|---|---|---|
| 汇总粒度 | **UTC 小时** | 支持任意整点时区；换时区不重算 | 天：只能出 UTC 报表；15 分钟：行数 ×4，只为极少数半点时区 |
| 汇总维度 | **按商品，不按品类** | 品类口径会变，商品销量不会变；改规则不重算小时表 | 按品类：查询少一次 IN，但每次改规则全量重算 |
| 分区 | **按月** | ClickHouse 建议分区值 100–1000 个，按月十年 120 个 | 按天：两年 730 个，合并不跨分区，易 Too many parts |

排序键以 `product_id` 打头：查询总是「一组商品 × 一段时间」，稀疏主键能直接跳到这些商品的数据块。

## 3 · 数据处理

代码：[`ingest/pipeline.py`](../src/salestrend/ingest/pipeline.py)、[`classify/rules.py`](../src/salestrend/classify/rules.py)。逐条结论见自动生成的[数据质量报告](data-quality-report.md)。

### 3.1 时区

- timestamp 不带时区。每个平台在 [`config/platforms.yaml`](../config/platforms.yaml) 配一个**源时区**和**是否已与采集方确认**（`{source_tz: UTC, confirmed: false}`），解析时转成 UTC 存储，原文保留在 `ts_raw`，改配置后可重放。旧写法 `app: UTC` 仍可解析，按未确认处理：默认未确认只会多一条警告，默认确认则可能把猜出来的时区当成事实、静默算错。
- 样例三个平台都是**未确认**，暂按 UTC 解析。依据：三个平台的时间戳都落在 07–22 点（另有 4 条月末边界探针），像同一时区的本地营业时间，但看不出是哪个时区。按 UTC 解析、按 UTC 出报表，等于**按时间戳上写的日期原样切日**，不管真实源时区是什么，这个结果都对。
- 真正的风险在请求的 `tz`。设源时间其实是北京时间：按 UTC 入库后再请求 `tz=Asia/Shanghai`，每条事件又被往后推 8 小时，2 月合计从 445 变成 **415**。所以 415 只在「源时间是 UTC」的假设下成立；如果源时间本来就是北京时间，按北京时间出报表应是 **445**，与原样切日相同（集成测试 `tests/integration/test_source_tz.py` 用管线 + 内存存储证明了这一点，不改全局配置）。
- 因此不拒绝请求，但会说出来：响应 `meta.source_tz_assumed` 表示选中平台里有未确认的源时区；当请求 `tz` 与某个未确认平台的源时区不同时，`meta.warnings` 带一条 `SOURCE_TZ_UNCONFIRMED`（`code`、`message`、`platforms`、`source_tz`），提示换算可能引入整体偏移。`tz` 与源时区相同时换算是恒等的，不告警。警告由配置算出、不来自数据，缓存命中时按当前配置重算。
- 默认 UTC 而不是 Asia/Shanghai：UTC 没有夏令时，假设错了也只是整体平移，不会一年两次跳变。
- 报表时区由请求的 `tz` 决定（IANA 名），只接受 IANA 名，不接受 `+08:00`：固定偏移处理不了夏令时。

**时区来源的三种情况**：

| 情况 | 含义 | 怎么处理 |
|---|---|---|
| 统一记 UTC | 采集端统一打 UTC 时间 | `source_tz: UTC, confirmed: true`，任何报表时区都是精确换算 |
| 各地按本地时间上报 | 每个平台（或站点）用自己所在地的本地时间 | 每个平台配自己的 IANA 源时区并确认；夏令时地区要求带偏移，见下 |
| 未确认 | 不知道是哪种 | **这份样例属于这一种**：按原样切日最安全，换时区出报表时告警 |

- **采集契约应要求时间戳带偏移量**（ISO 8601，例如 `2024-02-01T07:00-05:00`）。只给本地时间加一个时区名，在夏令时回拨那天 01:00–02:00 会出现两次，无法判断是哪一次；带了偏移量，每个时刻都能唯一换算成 UTC。源时区配置只用来兜底不带偏移的旧数据。
- **两种报表模式**：① 统一时钟——所有平台按同一个 `tz` 切日，用于汇总、对账；② 各市场本地日——每个市场按自己的本地日切，用于比较不同市场的购买行为（例如「各地晚上 8 点的高峰」）。汇总表按 UTC 小时存，这两种模式都不用重算：前者整表按一个时区切，后者按市场各自的时区切再拼起来。**当前只实现了统一时钟**。
- 样例里没有大区（region / market）字段。`app` / `marketplace` / `website` 是**渠道**，不等于地理区域，不能拿平台当市场去按本地日切；做第二种报表模式需要采集端补充区域字段。

### 3.2 去重

- **去重必须发生在聚合之前**。ClickHouse 的 Replacing 去重是后台合并时才发生的（最终一致）；增量物化视图在插入那一刻就把重复行聚合进去，事后救不回来。
- 幂等键：七个原始字段规范化（去首尾空白、平台与类目小写）后做 sha256。**标题不做归一化**：`Red Maxi Dress` 与 `Red Maxi-Dress` 是两次不同的上报。
- 批内去重 + 跨批去重（查本批时间窗内 fact 已有的 key）。`make ingest` 把同一文件再导一次，fact 行数不变。
- **只标不删**：raw 保留全部 300 行（`is_duplicate`、`dup_of`），fact 288 行。
- user_id 只是模拟字段，不能当幂等键。生产应要求采集方提供 `source + source_event_id`。

### 3.3 maxi dress 识别

规则文件：[`rules/maxi_dress.v1.yaml`](../rules/maxi_dress.v1.yaml)。

1. 四步归一化：小写 → 去所有格 → 标点转空格 → 折叠空格。
2. 按**词集合**判定，不按字符串：`LIKE '%maxi dress%'` 会漏掉 `Maxi Denim Dress`。
3. 规则自上而下命中即停，输出 match / exclude / review 与命中规则 id：
   - `R2` 有 skirt/top/pants… 且没有 dress → exclude（**Maxi Skirt 在这里排除**，纯关键词方案会多算 98 件）
   - `R1` 有 maxi 且有 dress/gown/sundress → match
   - `R3` 只有 long + dress → **review，不计入**（Elegant Long Dress：long 在 midi 到 maxi 之间都可能）
4. **原始 category 只做冲突告警，不做必要条件**：T-shirt Dress 被标成 shirt，说明类目本身不可全信。
5. 一个 product_id 的多种写法一起判，写法之间判定不一致时降级为 review。

| 选型 | 精度 | 可解释 | 判断 |
|---|---|---|---|
| ✅ 规则 + 词表 | 已知写法接近 100% | 每条判定指到规则 | 覆盖本数据全部情况 |
| 模糊匹配 | 中高 | 强 | Maxi Skirt 与 Maxi Dress 相似度本身就高，反而误判 |
| 向量 / 大模型 | 边界样本高 | 弱 / 不稳定 | 只适合裁决 review 长尾 |

**为什么规则优先**：口径必须可审计。业务问「为什么这件算 maxi dress」，规则能指到第几条。升级路径：规则 → 模糊匹配提召回 → 大模型裁决 review 项 → 稳定模式回灌成规则。

**规则版本化**：v1 与 v2（影子版本，把 long dress 也计入）并存，API 用 `rule_version` 切换。因为小时表按商品存，切版本**不重算事实与小时表**，只重算成员表和日表。

### 3.4 质量校验

| 规则 | 级别 | 处理 |
|---|---|---|
| product_id 非整数 / 时间无法解析 / 销量为负或非数字 / 平台未登记 | 拒收 | 只进 raw |
| 销量超过同商品其他行 P99 的 10 倍 | 告警 | 进 fact，报告列出 |
| 事件时间晚于入库时间 | 告警 | 进 fact，标 `future_ts` |
| 标题与类目冲突 | 告警 | T-shirt Dress 触发 |
| 本地时间 23:30–00:30 | 信息 | 日界敏感行，报告专节展示不同时区下的归属 |

## 4 · 预聚合与重算

代码：[`store.py`](../src/salestrend/store.py) 的 `rollup`，SQL 见 [`sql/rollup/`](../sql/rollup/)。

- **定时幂等重算，不用增量物化视图**。物化视图只在插入时触发，重复行一插进来就被聚合；也只由左表触发，维表（规则）变了不会重算。定时重算让迟到数据、去重修正、改规则走同一条路径。
- 重算整段**先删后写**，汇总存绝对值。只靠 Replacing 覆盖不够：某小时被修正成 0 时，新结果里根本没有那一行，旧行不会被覆盖。
- 生产节奏：每 5 分钟重算最近 3 小时，每天重算最近 2 天。演示数据是历史数据，用「数据最新时间」模拟当前时刻：`sealed_until = 2024-03-13 09:00Z`。
- 演进：量大后「最近 2 小时」改物化视图实时累加，「2 小时以前」仍由重算覆盖校正（Lambda 的批层与速度层）。

## 5 · 查询规划

代码：[`query/planner.py`](../src/salestrend/query/planner.py)，三段 SQL 见 [`sql/queries/`](../sql/queries/)。

1. 本地日期闭区间 → 逐日换算成 UTC 半开区间（夏令时切换日自然是 23 或 25 小时）。
2. 每个本地日分段：

| 段 | 条件 | 读哪张表 |
|---|---|---|
| A 日表 | tz 在预设列表，且该日完整落在 `sealed_until` 之前 | agg_class_daily |
| B 小时表 | tz 不在预设列表但当天是整点偏移，且已封口 | agg_product_hourly |
| C 明细 | 当天未封口；或非整点偏移且不在预设列表 | fact_sales |

3. **null 与 partial 按平台判**。每个平台有自己的截止时间（`data_through:{src}`，rollup 时取该平台最新事件），晚于它的日子对这个平台返回 **null**（还没数据），不是 0（没卖出去）；截止时刻所在的那一天对这个平台不完整。样例里 marketplace 截止 03-11 15:15Z、website 截止 03-12 18:05Z、app 截止 03-13 09:45Z，所以 marketplace 03-12 起、website 03-13 起为 null。
   - `total`：选中平台全部为 null → null；只有部分平台为 null → 非 null 平台之和，并把这一天加进 `partial_dates`。合计 `sum` 不受影响（null 按 0 计，本来就是 0）。
   - `partial_dates` = 任一选中平台的截止日 ∪ 部分平台已为 null 的日。
   - 查库范围以选中平台里最晚的截止为准；全部平台都为 null 的日不查库。
   - 响应 `meta.data_through_by_platform` 回显各平台截止。它是**推断值**：平台某天真的零销量时，会被误判成 null。生产环境应由采集侧上报心跳与接收水位（「这个平台的数据已经收到某时刻」），用它取代推断。
   - 只要有任何平台的 null 日，就按「未封口」走 60 秒 TTL：这些日子随时可能来数据。
4. 连续同段的日合并成一次查询；三段结果合并后在应用层统一补零。
5. 响应 `meta.plan` 回显各段覆盖的日期和读的表。

已封口读物化结果、未封口读明细，是 Druid、Timescale 实时聚合的通用思路；这里多做的一步是把**时区**也纳入分段条件。集成测试里有**对拍**：同一请求强制走三条路径，结果必须一致。

## 6 · API

OpenAPI：[openapi.json](openapi.json)。

```
GET /v1/product-classes/{class_slug}/sales-trend
    ?start_date=2024-01-01&end_date=2024-03-31   # 闭区间，最多 366 天
    &platforms=app,website                       # 逗号分隔，可选，默认全部
    &tz=Asia/Shanghai                            # IANA 名，默认 UTC
    &granularity=day                             # day | week | month
    &rule_version=v1                             # 默认当前版本
GET /v1/product-classes                          # 可查品类与规则版本
GET /v1/product-classes/{class_slug}             # 规则全文
GET /v1/meta/freshness                           # 封口水位、各平台数据截止
GET /healthz · /readyz
```

| 决定 | 选择 | 为什么 |
|---|---|---|
| 路径 | 品类作为资源 | `/maxi-dress-trend` 写死不可扩展；`?keyword=maxi` 每次口径不同，无法缓存与审计 |
| 日期 | 对外闭区间，对内半开 | 人说「1 号到 31 号」包含 31 号 |
| 出参 | 列式 `dates` + `series[].values` | 图表库直接用，体积约为行式三分之一 |
| 空值 | 0 与 null 区分 | 前端画折线时 null 断开、0 落地，含义不同 |
| 不完整周期 | week / month 加平行数组 `days_in_bucket` `days_covered` `partial` `daily_avg` | 3 月只到 13 号，按月合计 484 / 445 / 188 像「3 月崩了」，日均 15.6 / 15.3 / 14.5 其实基本持平 |
| 提示 | `meta.warnings` 数组，结构化 `code` + `message` | 不该拒绝、但调用方需要知道的情况（如源时区未确认），与错误码同一套命名 |
| 错误体 | RFC 9457 `application/problem+json` + `code` + `invalid_params` | 有标准，网关与前端都认识 |

**周 / 月桶的口径**：`days_in_bucket` 是桶本身的天数（周 7，月 28–31），不随查询区间裁剪；`days_covered` 是桶内落在查询区间、且不晚于数据截止（选中平台里最晚的一个）的天数，截止当天虽不完整也算一整天——样例 3 月正好是 13 天，日均 188 / 13 = 14.5，略偏保守；`partial` = `days_covered < days_in_bucket` 或桶内含 `partial_dates`；`daily_avg` = total / days_covered，一位小数，days_covered 为 0 时为 null。只给 total 的日均：各平台截止不同，按平台日均要用各自的覆盖天数，这次不做，调用方可用 `data_through_by_platform` 折算。`day` 粒度不带这四个字段：一天的桶没有「覆盖了几天」可言，不完整的日已经由 `partial_dates` 表达。

11 个错误码：`INVALID_DATE` `RANGE_INVERTED` `RANGE_TOO_LARGE` `UNKNOWN_PLATFORM` `INVALID_TZ` `INVALID_GRANULARITY`（400）、`CLASS_NOT_FOUND` `RULE_VERSION_NOT_FOUND`（404）、`RATE_LIMITED`（429，带 `Retry-After`）、`UPSTREAM_UNAVAILABLE`（503）、`QUERY_TIMEOUT`（504）。每个都有测试。

## 7 · 高并发与容错

代码：[`service.py`](../src/salestrend/service.py)、[`cache/resilience.py`](../src/salestrend/cache/resilience.py)。

| 问题 | 做法 |
|---|---|
| 存储扛不住 | Redis cache-aside。key 规范化（平台排序去重），含 `rule_version`、`data_gen` 与响应结构版本，改规则、回填或改响应结构后旧 key 自然作废，不用枚举删除 |
| 历史数据不变 | **分档 TTL**：区间全部已封口 24 小时；含未封口或未来日 60 秒；±10% 抖动防雪崩 |
| 缓存击穿 | singleflight：同 key 并发未命中只放一个请求查库 |
| 缓存穿透 | 品类列表进程内常驻，未知品类直接 404 不碰存储 |
| 突发流量 | 按 API key 令牌桶（默认 50/s，突发 100） |
| 存储挂了 | 查询超时 2 秒；连续失败熔断 30 秒；有旧缓存返回旧数据并标 `stale: true` + `Warning: 110`，没有则 503 / 504 |
| Redis 挂了 | 缓存读写失败一律当未命中，直接查存储，不影响正确性 |
| 客户端重复拉取 | `ETag` + `If-None-Match` → 304 |

**降级取舍**：存储挂了时返回旧数据还是报错？返回旧数据并明确标记。看板场景下，一个标着「数据可能延迟」的图比报错页有用；标记保证调用方不会把旧数据当新数据。

### 7.1 容量假设与 SLO（生产）

| 量 | 假设 |
|---|---|
| 日增明细 | 1000 万行 |
| SKU / 平台 | 30 万 / 10 |
| 峰值 | 5000 在线 × 6 卡片 ÷ 30 秒 = 1000 QPS，× 2 冗余 = **2000 QPS** |
| 请求组成 | 70% 热点（命中缓存）· 25% 自定义（日表段）· 5% 重查询 |

SLO：2000 QPS 下 P95 ≤ 80 ms、P99 ≤ 200 ms，缓存命中率 ≥ 90%，5xx < 0.1%。本机实测见[压测报告](perf-report.md)。

### 7.2 扩展路线

| 档 | 规模 | 变化 |
|---|---|---|
| S0 | 笔试演示，300 行 | 单机 docker |
| S1 | 日增 100 万以内 | 单节点 ClickHouse，或直接 PostgreSQL |
| S2 | 日增 1000 万 | ClickHouse 双副本；API 无状态水平扩展；Redis 主从；singleflight 锁放 Redis；最近 2 小时改物化视图 |
| S3 | 日增 1 亿以上 | 按 product_id 哈希分片；明细 13 个月后 TTL MOVE 到对象存储；热门品类日表推到 CDN 边缘 |

**冷热**：汇总表很小，永久留在热区；冷区间查询只读汇总表，不碰冷明细，所以冷数据对趋势接口透明。
