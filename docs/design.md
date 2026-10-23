# 系统与数据设计说明

> 每个选型按「为什么 / 反方 / 何时换」写。代码位置用相对路径标出，方便对照。

## 0 · 一句话

写入时把脏活干完（时区、去重、质量标记），汇总分两层物化：「商品 × 平台 × UTC 15 分钟」管灵活性、当重算中间层，「品类 × 规则版本 × 平台 × UTC 15 分钟 / 预设时区本地日」管热路径；查询时由规划器把日期区间按 UTC 半开区间拆成「品类日表 / 品类 15 分钟表 / 明细」三段（一天之内也切：明细只读封口线之后的尾巴），每段用最便宜的表，再叠一层 Redis。

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
| fact_sales | ReplacingMergeTree | **event_ts**, product_id, event_key | 事件月 | 干净行；Replacing 只是兜底 |
| dim_product | ReplacingMergeTree | product_id | — | 以 id 为锚，记录全部标题写法 |
| product_class_member | ReplacingMergeTree | class_slug, rule_version, product_id | — | 规则引擎输出，多版本并存 |
| agg_product_15m | ReplacingMergeTree | product_id, bucket_15m, source | 月 | 商品 × 平台 × UTC 15 分钟；**重算中间层，不在查询路径上** |
| agg_class_15m | ReplacingMergeTree | class_slug, rule_version, bucket_15m, source | 月 | 品类 × 规则版本 × 平台 × UTC 15 分钟；B 段热路径 |
| agg_class_daily | ReplacingMergeTree | class_slug, rule_version, tz, local_date, source | 月 | 预设时区的品类日表；A 段热路径 |
| pipeline_state | ReplacingMergeTree | key | — | 封口水位（按 15 分钟对齐）与数据截止时间 |

四个关键决定：

| 决定 | 选择 | 为什么 | 反方 |
|---|---|---|---|
| 汇总粒度 | **UTC 15 分钟** | 现行全部时区的偏移都是 15 分钟的整数倍（+05:45 Kathmandu、+08:45 Eucla、+12:45 Chatham），15 分钟桶能切准任何时区的日界：非整点、不在预设里的时区也不扫明细；未封口那天也能在封口线处切开 | 小时：行数 ¼，但 +05:45 这类时区只能整段扫明细（v1.2.0 的做法）。P5 实测品类层一年区间小时 vs 15 分钟：8 并发 P99 14 vs 15 ms，16 并发 21 vs 27 ms（同一库、同一轮手工 benchmark），差距可以忽略 |
| 汇总维度 | **两层：按商品存一层，再按品类物化一层** | 品类口径会变，商品销量不会变：商品层改规则不重算；品类层很小（一个规则版本一年至多 3 × 35,136 ≈ 10.5 万行，P5 实测 70,272 行），改规则时从商品层重算它只要 0.2 秒 | 只按商品：查询时现场 IN 过滤，稀疏数据下是全表扫描（见下）；只按品类：每次改规则要从明细全量重算 |
| 分区 | **按月** | ClickHouse 建议分区值 100–1000 个，按月十年 120 个 | 按天：两年 730 个，合并不跨分区，易 Too many parts |
| 明细排序键 | **event_ts 打头** | 明细在查询路径上只剩「封口线之后的尾巴」，重算也按时间区间扫，两者都是「全部商品 × 一小段时间」 | product_id 打头（v1.2.0）：适合「一组商品 × 长时间」，但品类商品分散时一个 granule 也跳不过，见下 |

**为什么商品层不能留在查询路径上（P5 实测）**：排序键以 `product_id` 打头，本意是让稀疏主键直接跳到品类商品的数据块。这只在品类商品成片聚集时成立。品类占 10% 且分散时，一个 granule（8192 行）横跨约 50 个商品，每个 granule 都含命中商品，一个也跳不过——一年区间的商品层查询就是全表扫描。另外「商品 × 时间桶」在稀疏数据下压不掉行数（1000 万事件汇总成 15 分钟后 986 万行，小时粒度也有 945 万），省的只是字节。

| 层 | 表 | 管什么 | 在不在查询路径上 |
|---|---|---|---|
| 商品层 | agg_product_15m | 灵活性：品类口径会变、商品销量不会变，改规则不重算它和明细；重算品类层的上游 | **不在**，只当重算中间层（`product_15m` 只是对拍用的 `force` 选项） |
| 品类层 | agg_class_15m、agg_class_daily | 热路径：一个规则版本一年 70,272 / 6,588 行（P5），排序键以 `class_slug, rule_version` 打头，读十几个 granule | A、B 两段 |

P5 对照（一年 Europe/Berlin，带 FINAL，8 并发 P99）：商品层 1,453 ms（每月 4–5 个 part）/ 221 ms（合并后）→ 品类层 24 / 13 ms。

**明细排序键为什么改成 event_ts 打头（P5 实测）**：实时请求原来是「当天没封口，整天读明细」。改成封口线之前读品类层、只有尾巴读明细之后，在 product_id 打头的明细上**一行都没省**：查 12-31 整天与查 12-31 12:00 之后的半天，read_rows 都是 851,115（整个 12 月分区），8 并发 P99 51 / 47 ms。原因同上，时间条件在 product_id 打头的排序键上跳不过 granule。改成 `(event_ts, product_id, event_key)` 后，同一份 1000 万行数据：半天尾巴读 31,915 行、整天 40,107 行（少 21–27 倍），一个月 824,118 行（不变），压缩后体积 +2.6%。去重键随之变成 (event_ts, product_id, event_key)：重复行这三列全同，Replacing 的去重语义不变。延迟（clickhouse-benchmark，同一份数据，均已合并）：尾巴查询 8 并发 P99 56 → 15 ms、吞吐 233 → 841 QPS，16 并发 113 → 24 ms；端到端效果见 §7.1。

**FINAL 的取舍（P5 实测，已改正 v1.2.0 的结论）**：查询都带 `FINAL`，用来兜底 Replacing 表里尚未合并的重复行；重复只会来自两种竞态——并发导入同一批数据（「查已有 key → 写入」不是原子的），以及两个重算任务重叠（「先删后写」是两条语句）。v1.2.0 写的是「主要成本是 FINAL」，**这不准确**：

- 商品层查询慢的**根因是全表扫描**（每次读 986 万行）：合并完、不加 FINAL，8 并发 P99 也有 237 ms。
- **FINAL 的代价随 part 数增长，part 合并后可以忽略**：重算后每个月 4–5 个 part（共 60 个）时，FINAL 要在查询时跨 part 归并，商品层 8 并发 P99 1,453 ms、内存 222 MB、16 并发 160 个查询里 44 个内存超限；`OPTIMIZE … FINAL` 压成每月 1 个 part 后是 221 ms、0.8 MB，与不加 FINAL（237 ms）几乎一样。明细段同理：31 天 8 并发 P99 475 → 95 ms。
- v1.2.0 的 1.2 秒 = 全表扫描 × 分片未合并让 FINAL 放大 × 4 核上 8 个查询排队。

| 位置 | 决定 |
|---|---|
| 重算 SQL 读 fact_sales、agg_product_15m | 保留 FINAL：每轮重算付一次，保证汇总建在去重后的结果上 |
| 日表段、品类 15 分钟表段 | 保留：一年几千到七万行，有无 FINAL 无差别 |
| 商品层查询 | 不再存在：它已不在查询路径上，v1.2.0 提的「去掉 FINAL + 整分区原子替换」不再需要 |
| 明细段 | 保留：并发导入的竞态只能靠它兜底；part 合并后额外代价可以忽略。运维上大批量回填后先等合并（或主动 OPTIMIZE）再放流量 |

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
- **两种报表模式**：① 统一时钟——所有平台按同一个 `tz` 切日，用于汇总、对账；② 各市场本地日——每个市场按自己的本地日切，用于比较不同市场的购买行为（例如「各地晚上 8 点的高峰」）。汇总表按 UTC 15 分钟存，这两种模式都不用重算：前者整表按一个时区切，后者按市场各自的时区切再拼起来。**当前只实现了统一时钟**。
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

**规则版本化**：v1 与 v2（影子版本，把 long dress 也计入）并存，API 用 `rule_version` 切换。因为商品层按商品存，切版本**不重算事实与商品层**，只重算成员表、品类 15 分钟表和品类日表。

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
- 顺序：商品 15 分钟表（从明细，[`product_15m.sql`](../sql/rollup/product_15m.sql)）→ 品类 15 分钟表（从商品层汇总，join 成员表的 match，[`class_15m.sql`](../sql/rollup/class_15m.sql)）→ 品类日表（从明细）。品类 15 分钟表**每个已加载的规则版本都物化**（v1、v2、v3）：一个版本一年至多 3 平台 × 35,136 格 ≈ 10.5 万行，P5 全年重算 0.2 s，规划器也就不用区分「这个版本有没有品类表」。
- **规则改版**：只重算成员表和两张品类表，商品层和明细都不动。这继承了「汇总按商品存，改规则不重算明细」的原则，同时让热路径直接读品类层。
- **封口线按 15 分钟对齐**：`sealed_until` = 当前时刻向下取到 15 分钟格（v1.2.0 是取整点）。品类表就是 15 分钟粒度，封口线落在格上才能在一天之内切开；对齐到 15 分钟而不是整点，未封口的尾巴更短。实现成本为零：重算区间本来就是 `[start, sealed)` 整段先删后写。
- 生产节奏：每 5 分钟重算最近 3 小时，每天重算最近 2 天；明细尾巴因此至多「15 分钟 + 一个重算间隔」。演示数据是历史数据，用「数据最新时间」模拟当前时刻：`sealed_until = 2024-03-13 09:45Z`。
- 演进：量大后「最近 2 小时」改物化视图实时累加，「2 小时以前」仍由重算覆盖校正（Lambda 的批层与速度层）。

## 5 · 查询规划

代码：[`query/planner.py`](../src/salestrend/query/planner.py)，三段 SQL 见 [`sql/queries/`](../sql/queries/)。

1. 本地日期闭区间 → 逐日换算成 UTC 半开区间（夏令时切换日自然是 23 或 25 小时）。
2. 每个本地日切成一块或两块，**按 UTC 半开区间切，一天之内也切**：

| 段 | 条件 | 读哪张表 |
|---|---|---|
| A 日表 | tz 在预设列表，且该日完整落在 `sealed_until` 之前 | agg_class_daily |
| B 15 分钟表 | 其余已封口的部分：非预设时区的已封口日（**含 +05:45 这类非整点时区**），以及未封口那天里 `[日始, sealed_until)` 这一段 | agg_class_15m |
| C 明细 | `[sealed_until, 日终)` 这段尾巴；以及日界不落在 15 分钟格上的零头（只有 1972 年前的地方平时，例如 Africa/Monrovia −0:44:30，现行时区没有） | fact_sales |

首尾相接的同类块合并成一次查询（例如 Berlin 的历史日与当天前半天合成一段 B），`meta.plan` 每段回显 `utc_from` / `utc_to`，一天被切开时能看到切在哪里。商品层只当重算中间层，`product_15m` 只是对拍用的 `force` 选项，规划器自己不会选它。

**C 段区间上限与单独限流：核实与重新评估**。`trend_fact.sql` 的注释一直写着「API 层单独限流，区间上限 31 天」，核实发现 v1.2.0 **两条都没落实**：参数校验只有 366 天的总上限，令牌桶也只有一个，`tz=Asia/Kathmandu` 查一年会把一年明细扫一遍。本轮先按注释补上了这两条（31 天上限返回 400、重查询单独一个令牌桶），随后改成 15 分钟粒度——非整点时区的已封口日也读品类表，明细只剩封口线之后的尾巴，任何请求都造不出长区间的明细扫描。所以**两条都撤掉了**：再留着只会把本来能毫秒级返回的 Kathmandu 一年请求拒成 400。剩下的风险是**重算停摆**：`sealed_until` 不前进时，所有请求的尾巴一起变长。这不该由单个请求的限额兜，应该监控 `now − sealed_until`（建议 > 30 分钟告警）。

3. **null 与 partial 按平台判**。每个平台有自己的截止时间（`data_through:{src}`，rollup 时取该平台最新事件），晚于它的日子对这个平台返回 **null**（还没数据），不是 0（没卖出去）；截止时刻所在的那一天对这个平台不完整。样例里 marketplace 截止 03-11 15:15Z、website 截止 03-12 18:05Z、app 截止 03-13 09:45Z，所以 marketplace 03-12 起、website 03-13 起为 null。
   - `total`：选中平台全部为 null → null；只有部分平台为 null → 非 null 平台之和，并把这一天加进 `partial_dates`。合计 `sum` 不受影响（null 按 0 计，本来就是 0）。
   - `partial_dates` = 任一选中平台的截止日 ∪ 部分平台已为 null 的日。
   - 查库范围以选中平台里最晚的截止为准；全部平台都为 null 的日不查库。
   - 响应 `meta.data_through_by_platform` 回显各平台截止。它是**推断值**：平台某天真的零销量时，会被误判成 null。生产环境应由采集侧上报心跳与接收水位（「这个平台的数据已经收到某时刻」），用它取代推断。
   - 只要有任何平台的 null 日，就按「未封口」走 60 秒 TTL：这些日子随时可能来数据。
4. 首尾相接的同段合并成一次查询；各段结果按本地日累加后在应用层统一补零（同一天可能分属 B、C 两段）。
5. 响应 `meta.plan` 回显各段覆盖的本地日期、UTC 区间和读的表。

已封口读物化结果、未封口读明细，是 Druid、Timescale 实时聚合的通用思路；这里多做的一步是把**时区**也纳入分段条件。集成测试里有**对拍**：同一请求强制走日表 / 品类 15 分钟表 / 商品 15 分钟表 / 明细四条路径，结果必须一致；Berlin、Kathmandu（+05:45）、Eucla（+08:45）走品类 15 分钟表的逐日结果与扫明细一致；v2、v3 同样一致；当天在封口线处切开后的逐日结果与整段扫明细一致。

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
| 存储挂了 | 查询超时 2 秒；连续失败熔断 30 秒；有旧缓存返回旧数据并标 `meta.stale: true` + `X-Data-Stale: true` 响应头，没有则 503 / 504 |
| Redis 挂了 | 缓存读写失败一律当未命中，直接查存储，不影响正确性 |
| 客户端重复拉取 | `ETag` + `If-None-Match` → 304 |
| 下游 HTTP 缓存 | `Cache-Control: private, max-age=3600 / 30`（按 TTL 档位），降级响应 `no-cache` |

**降级取舍**：存储挂了时返回旧数据还是报错？返回旧数据并明确标记。看板场景下，一个标着「数据可能延迟」的图比报错页有用；标记保证调用方不会把旧数据当新数据。

**旧数据怎么标**：以响应体 `meta.stale` 为准，另带 `X-Data-Stale: true` 响应头，方便网关和访问日志不解析 body 就能过滤。不用 `Warning: 110`：RFC 9111 已把 `Warning` 头整体废弃，浏览器和多数代理不再处理它。

**为什么 `private` 不是 `public`**：响应按租户（`X-API-Key`）区分，`public` 允许 CDN 和共享代理缓存，第一个租户的响应会被原样返回给下一个租户。`private` 只允许调用方自己的浏览器缓存。**要上 CDN，必须二选一**：响应加 `Vary: X-API-Key`（CDN 按租户分别缓存，命中率随租户数下降），或把租户放进 URL（如 `/v1/tenants/{tenant}/...`，缓存 key 天然带租户）。在此之前保持 `private`。

### 7.1 容量假设与 SLO（生产）

| 量 | 假设 |
|---|---|
| 日增明细 | 1000 万行 |
| SKU / 平台 | 30 万 / 10 |
| 峰值 | 5000 在线 × 6 卡片 ÷ 30 秒 = 1000 QPS，× 2 冗余 = **2000 QPS** |
| 请求组成 | 70% 热点（命中缓存）· 25% 自定义（日表段）· 5% 重查询 |

SLO：2000 QPS 下 P95 ≤ 80 ms、P99 ≤ 200 ms，缓存命中率 ≥ 90%，5xx < 0.1%。本机实测见[压测报告](perf-report.md)。

**延迟目标（端到端，缓存未命中）**：已聚合的数据（日表段、品类 15 分钟表段）响应 **50–100 ms 以内**；含当天实时数据的响应（封口线之前读汇总、之后的尾巴读明细）**≤ 150 ms**。P5 实测（1000 万行明细，缓存全部旁路，2 个 API worker；k6、API、ClickHouse 在同一台机器上，API 与 ClickHouse 共用 4 vCPU 虚拟机，结果偏保守；详见[压测报告](perf-report.md)）：

| 路径 | 目标 P99 | 10 / 50 / 100 / 200 / 300 RPS 的 P99 ms | 仍满足目标的最高到达率 |
|---|---|---|---|
| 已聚合 · 一年 · UTC（日表） | 100 | 27 / 14 / 21 / 83 / 14 | 300 RPS ✅ |
| 已聚合 · 一年 · Europe/Berlin（15 分钟表） | 100 | 27 / 11 / 10 / 14 / 27 | 300 RPS ✅ |
| 已聚合 · 一年 · Asia/Kathmandu（15 分钟表） | 100 | 29 / 25 / 12 / 115 / 303 | 100 RPS（200 RPS ❌，判断为单轮波动，照实记） |
| 实时 · 近 30 天 · UTC | 150 | 35 / 23 / 28 / 21 / 106 | 300 RPS ✅（优化前 200） |
| 实时 · 近 30 天 · Europe/Berlin | 150 | 37 / 19 / 61 / 68 / 56 | 300 RPS ✅（优化前 200） |
| 实时 · 近 30 天 · Asia/Kathmandu | 150 | 36 / 18 / 20 / 78 / 157 | 200 RPS（300 RPS 157 ms ❌；优化前 300 RPS 有 15.8% 超时） |
| 缓存命中（20 个热点 key） | 20 | 250–1,500 RPS 全部 ≤ 24 ms | ≥ 1,500 RPS ✅（压测机端口转发的上限，不是服务拐点） |

- 已聚合：单机 300 RPS 内 P99 ≤ 27 ms，达成「50–100 ms 以内」✅（Kathmandu 200 RPS 一档除外）。
- 实时：200 RPS 内全部 ≤ 78 ms ✅；300 RPS 时 UTC、Berlin 达标，Kathmandu 157 ms 略超 ❌。
- 实时的优化（当天按封口线切开 + 明细 event_ts 打头）在 SQL 层把明细段 8 并发 P99 从 61 降到 15 ms、吞吐 ×4.5；端到端拐点从 200 RPS 提到 300 RPS。
- 拐点下一档时虚拟机 CPU 约 2/3 到 4/5 忙（API + ClickHouse 合计 260–330%），k6 在宿主机上只用 10–20%。已聚合路径 API 与 ClickHouse 各半，实时路径 ClickHouse 约是 API 的 2 倍。

存储层分段目标（单段 SQL 服务端 P99）：日表段一年 ≤ 150 ms，品类 15 分钟表段一年 ≤ 500 ms，明细段 31 天 ≤ 1500 ms。P5 在 16 并发下全部达标（合并后：日表 15 ms、品类 15 分钟表 23 ms、明细 31 天 226 ms）。

### 7.2 可观测性：`/metrics`

代码：[`metrics.py`](../src/salestrend/metrics.py)。`/metrics` 不走限流（抓取方被 429 等于监控失明），不进 OpenAPI。

| 指标 | 类型 | 标签 | 记什么 |
|---|---|---|---|
| `trend_requests_total` | counter | `cache`=hit / miss / stale / none，`status` | 趋势接口请求数。503 / 504 记 miss（缓存未命中后存储失败）；参数错、限流、404 没走到缓存，记 none |
| `trend_request_seconds` | histogram | `path`=cache_hit / class_daily / class_15m / fact / mixed / stale / empty（v1.2.0 的 `hourly` 改名为 `class_15m`） | 成功响应的耗时。多段计划记 mixed；empty = 区间全部晚于数据截止、不查库。桶 1ms–5s |
| `storage_queries_total` | counter | `segment` | 每成功执行一段 SQL 加 1 |
| `singleflight_shared_total` | counter | — | 被 singleflight 合并、等别人结果的请求数 |
| `circuit_open` | gauge | — | 1 = 熔断中。多 worker 取最大值：任一 worker 熔断即为 1 |

**多 worker**：uvicorn 起 2 个 worker，各是独立进程。不做处理时 `/metrics` 只返回恰好接到抓取请求的那个 worker 的数，命中率和总数都是错的。所以用 prometheus_client 多进程模式：Dockerfile 设 `PROMETHEUS_MULTIPROC_DIR` 并在启动时清空，每个 worker 把计数写进目录下按 pid 命名的文件，`/metrics` 用 `MultiProcessCollector` 汇总。实测：清空后打 300 个请求，两个 worker 各自记了 135 和 166（第 301 个是另一次 curl），`/metrics` 汇总为 300。

**公式**（PromQL，5 分钟窗口）：

```
命中率 = sum(rate(trend_requests_total{cache="hit"}[5m]))
       / sum(rate(trend_requests_total{cache=~"hit|miss|stale"}[5m]))
回源率 = sum(rate(storage_queries_total[5m])) / sum(rate(trend_requests_total{cache!="none"}[5m]))
```

- 命中率的分母只含走到缓存的请求（hit + miss + stale），参数错之类不算。stale 计入分母、不计入分子：降级返回的是旧数据，不算命中。
- 回源率按「SQL 段数 ÷ 请求数」算，一个混合计划算 2–3 次回源，所以它可以大于 1 − 命中率；被 singleflight 合并的请求记作 miss 却不产生回源，又会让它小于 1 − 命中率。两者的差值本身就是诊断信号。
- **告警线**：命中率 < 85% 持续 10 分钟**预警**，< 70% **告警**（SLO 要求 ≥ 90%）。另配：`circuit_open == 1` 立即告警；`histogram_quantile(0.99, ...{path!="cache_hit"})` > 1s 预警。
- 命中率高度依赖用户怎么选区间：看板预设区间（20 个热点 key）与任意区间差别很大，实测对照见[压测报告](perf-report.md) P6。

### 7.3 扩展路线

| 档 | 规模 | 变化 |
|---|---|---|
| S0 | 笔试演示，300 行 | 单机 docker |
| S1 | 日增 100 万以内 | 单节点 ClickHouse，或直接 PostgreSQL |
| S2 | 日增 1000 万 | ClickHouse 双副本；API 无状态水平扩展；Redis 主从；singleflight 锁放 Redis；最近 2 小时改物化视图 |
| S3 | 日增 1 亿以上 | 按 product_id 哈希分片；明细 13 个月后 TTL MOVE 到对象存储；热门品类日表推到 CDN 边缘 |

**冷热**：汇总表很小，永久留在热区；冷区间查询只读汇总表，不碰冷明细，所以冷数据对趋势接口透明。

### 7.4 容量规划：单机拐点与万级推算

**利特尔法则**：系统里同时在处理的请求数 L = 到达率 λ × 平均响应时间 W。「万级并发」常被混用成两件事，要先换算清楚：

| 说法 | 换算 | 结果 |
|---|---|---|
| 1 万同时在线用户 | 每人每 30 秒刷新一次看板、一次看板发 6 个请求：λ = 10,000 × 6 ÷ 30 | **2,000 QPS** |
| 1 万 QPS | 同样的使用习惯：在线用户 = 10,000 × 30 ÷ 6 | 约 **5 万在线用户** |
| 1 万 QPS 时服务端同时在处理的请求 | L = λ × W；W 按命中 1 ms、未命中 10 ms 加权（90% 命中时 W ≈ 1.9 ms） | **约 20 个**；即使全部未命中（W ≈ 10 ms）也只有约 100 个 |

所以「1 万同时在线」对服务端只是 2,000 QPS、几十个在途请求，压力来自 QPS 与命中率，不来自连接数。下面按 **1 万 QPS** 推算。

**公式**（λ = 10,000，h = 命中率；未命中里约 97% 是只读汇总的请求、约 3% 是含当天的混合路径，取自 P6 实测）：

```
API 核数 = λ·h·c_hit + λ·(1−h)·c_api_miss
CH  核数 = λ·(1−h)·c_ch_miss
节点数   = ⌈核数 ÷ (每台 vCPU × 0.6)⌉      # 留 40% 余量守 P99：单机拐点出现在虚拟机 CPU 约 65–80% 时
```

**实测输入**（P5 与 `make capacity`，`docker stats` 采样，1 核 = 100%，均为达标的最高一档）：

| 参数 | 值 | 来源 |
|---|---|---|
| c_hit（API 每个命中请求） | **0.7 ms·核** | 命中路径 1,500 RPS 时 API 104%（2 worker）/ 101%（4 worker） |
| c_api_miss（API 每个未命中请求） | **3.0 ms·核** | 日表段 300 RPS 时 API 89%；实时 UTC 300 RPS 时 92%，按 97 / 3 加权 |
| c_ch_miss（ClickHouse 每个未命中请求） | **3.2 ms·核** | 日表段 300 RPS 时 ClickHouse 93%（3.1）；实时 UTC 176%（5.9），按 97 / 3 加权 |

**推算**（4 vCPU 一台可用 2.4 核；16 vCPU 一台可用 9.6 核）：

| 命中率 | 命中 / 未命中 QPS | API 核数 | ClickHouse 核数 | API 节点（4 vCPU / 16 vCPU） | ClickHouse 节点（4 vCPU / 16 vCPU） |
|---|---|---|---|---|---|
| 99% | 9,900 / 100 | 7.2 | 0.3 | 4 / 1 | 1 / 1（高可用至少 2 副本） |
| 90% | 9,000 / 1,000 | 9.3 | 3.2 | 4 / 1 | 2 / 1 |
| 50% | 5,000 / 5,000 | 18.5 | 16.0 | 8 / 2 | 7 / 2 |
| 11%（P6 任意区间实测） | 1,100 / 8,900 | 27.4 | 28.5 | 12 / 3 | 12 / 3 |

- **ClickHouse 能不能近似线性扩展**：热路径查询每次只读几千到几万行、彼此独立，靠**加副本**（不是分片）就能按台数近似线性扩 QPS——每个副本都有全量的小表，负载均衡到任意副本即可。单台从 4 vCPU 换 16 vCPU 不是严格线性：小查询的固定开销（解析、调度、线程启动）不随核数摊薄，P5 上日表段 8 → 16 并发 QPS 只从 227 涨到 284。所以表里 16 vCPU 的节点数偏乐观，规划时按 4 vCPU 小机多副本更稳。分片只在明细量本身扛不住时才需要（§7.3 的 S3）。
- **API**：无状态，按节点线性扩。每个 uvicorn worker 是一个 Python 进程，在 GIL 下实测约能用满 0.7–0.8 核，所以 worker 数应接近核数。本机 2 个与 4 个 worker 在 1,500 RPS 命中路径下没有可见差别（API 才用了约 1 核），**默认值保持 2**，已改成环境变量 `API_WORKERS`；生产按「worker 数 ≈ vCPU 数」配置，16 vCPU 的 API 节点要开 12–16 个 worker 才用得满。
- **Redis**：每个请求 2 次 GET（数据代号 + 响应体），未命中再加 2 次 SET（新鲜副本 + stale 副本）。1 万 QPS 时 ≤ 4 万 ops/s，单实例（约 10 万 ops/s）够用；瓶颈在带宽与内存：响应体 2–9 KB（近 30 天 1.9 KB、90 天 3.4 KB、一年 9.2 KB），1 万 QPS × 3.4 KB ≈ 34 MB/s，要千兆以上网卡；内存按 key 数算，P6 那种任意区间约 34 万个 key × 3.4 KB × 2 份 ≈ 2.3 GB（24 小时 TTL）。规格：**1 主 1 从、4 GB 内存、万兆网卡**。命中率越低，Redis 越不是瓶颈，ClickHouse 才是。
- **局限**：c_hit 来自 1,500 RPS（压测机的上限，不是拐点），可能偏高（固定开销被摊得不够）；c_api_miss / c_ch_miss 来自同机测量，k6 与服务抢宿主机资源。推算是量级，不是承诺；上线前应在独立压测机上复测。
