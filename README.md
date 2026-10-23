# sales-trend · 销售数据趋势查询与 API

万悉科技笔试题一：从多平台销售明细里，按任意日期区间、一个或多个平台、任意时区，返回「maxi dress」的日销量趋势，并在大数据量、高并发下保持可用。

## 三条命令跑起来

**前置依赖**

| 用途 | 需要 |
|---|---|
| `make up` / `seed` / `ingest` / `rollup` | Docker（含 compose v2）；首次运行要联网拉取 `clickhouse/clickhouse-server:24.8`、`redis:7-alpine`、`python:3.12-slim` 与 uv 镜像 |
| `make test` / `test-unit` / `report` / `openapi` | 宿主机装 [uv](https://docs.astral.sh/uv/)（它会按 `uv.lock` 拉起 Python 3.12+ 与依赖） |
| `make demo` | `python3` 与 `curl` |
| `make perf` / `perf-p5` / `perf-p6` / `capacity` | 另需 [k6](https://k6.io/) |

```bash
git clone https://github.com/yuhangziyue/sales-trend.git
cd sales-trend
make up      # 起 ClickHouse 24.8、Redis 7、API 三个容器
make seed    # 建表 → 导入 xlsx → 去重与质量校验 → 品类识别 → 汇总重算
make demo    # 10 条典型请求与真实响应
```

然后打开 <http://localhost:8000/docs> 交互式调用；<http://localhost:8000/metrics> 是 Prometheus 指标（多 worker 汇总，口径见 [design.md §7.2](docs/design.md)）。

**端口被占用时**：宿主机端口都可以用环境变量覆盖，`CH_HOST_PORT`（默认 8123）、`REDIS_HOST_PORT`（6379）、`API_HOST_PORT`（8000），例如 `API_HOST_PORT=18000 CH_HOST_PORT=18123 make up`。`make demo` 跟随 `API_HOST_PORT`；`make test` 在宿主机上直连 ClickHouse，改过端口要同时设 `CH_PORT`（如 `CH_PORT=18123 make test`）。

其他命令：

| 命令 | 做什么 |
|---|---|
| `make test` | 单元 + 集成测试。**集成测试要求数据库已经起来并灌好样例**（先 `make up && make seed`）：连不上 ClickHouse 时直接失败，不会静默跳过；只想跑单元测试用 `make test-unit`，或显式 `SKIP_INTEGRATION=1 make test` |
| `make report` | 重新生成[数据质量报告](docs/data-quality-report.md)（不需要数据库） |
| `make ingest` | 把同一文件再导一次，演示跨批去重：fact 行数不变 |
| `make perf` | k6 固定到达率压测（P1–P4、P6），生成[压测报告](docs/perf-report.md)（需要本机装 k6） |
| `make perf-p5` | 千万级实测：独立库 `p5` 合成 1000 万行明细，测存储、重算、三段查询与端到端，测完删库（`KEEP=1` 保留） |
| `make perf-p6` | 只跑 P6：任意区间命中率（两层缓存分三档统计，附每 10 秒一个窗口的冷启动曲线），附 20 个热点 key 的对照；`BLOCK_CACHE=0 P6_SCENS=range P6_SUFFIX=_noblock make perf-p6` 跑只有整响应一层的对照 |
| `make capacity` | 容量：缓存命中路径逐档加压找单机拐点，对照 2 / 4 个 API worker（`API_WORKERS`） |
| `make down` | 清理容器与数据 |

## 结果一览（样例数据，去重后）

| 口径 | 值 |
|---|---|
| maxi dress 命中商品 | 2001–2012，共 12 个 |
| 总销量（UTC） | **1117**：app 368 · marketplace 372 · website 377 |
| 分月（UTC） | 1 月 484 · 2 月 445 · 3 月 188 |
| 分月日均（UTC） | 15.6 · 15.3 · 14.5（3 月只覆盖 1–13 号，`partial: true`；合计 188 不是「崩了」） |
| 2 月合计，按 Asia/Shanghai 出报表 | **取决于源时区**：源时间若是 UTC，为 415（跨月探针行换日）；源时间若本来就是北京时间，为 445，与原样切日相同。源时区尚未确认，所以这个请求会带 `SOURCE_TZ_UNCONFIRMED` 警告 |
| 原始 300 行 | 12 行整行重复，fact 288 行 |
| 边界商品 | Maxi Skirt 排除 · Elegant Long Dress 待审不计入 · T-shirt Dress 类目冲突告警 |
| 数据截止 | 按平台推断：app 03-13 09:45Z · website 03-12 18:05Z · marketplace 03-11 15:15Z；各平台截止之后返回 `null` 而不是 `0` |

这些数字全部写在 [`tests/golden/expected.json`](tests/golden/expected.json)，集成测试经 API 全链路逐条断言。

**千万级实测（P5，合成 1000 万行明细，4 vCPU 虚拟机，详见[压测报告](docs/perf-report.md)）**：

| 项 | 结果 |
|---|---|
| 存储 | 明细 426 MB（压缩比 1.36，随机 event_key 占大头）· 商品 15 分钟层 986 万行 60 MB（重算中间层）· 品类 15 分钟表 70,272 行 · 品类日表 6,588 行 |
| 重算 | 全年商品 15 分钟层 2.0 s；品类 15 分钟表 0.2 s；品类日表每个预设时区全年 0.2–0.6 s |
| 日表段 · 一年 | SQL P99 串行 1 ms / 8 并发 7 ms / 16 并发 15 ms ✅ |
| 品类 15 分钟表段 · 一年（Berlin / Kathmandu +05:45） | SQL P99 8 并发 13 / 12 ms，16 并发 23 / 23 ms ✅（v1.2.0 读商品小时表：8 并发 1,248 ms；Kathmandu 只能扫明细） |
| 明细尾巴（实时段） | 明细改为 event_ts 打头后，半天尾巴读 3.2 万行（原 85 万），8 并发 P99 56 → 15 ms |
| 端到端 · 已聚合（缓存全部未命中，目标 50–100 ms） | 一年区间：UTC 日表、Berlin 15 分钟表 300 RPS 下 P99 14 / 27 ms ✅；Kathmandu 100 RPS ✅、200 RPS 115 ms ❌ |
| 端到端 · 含当天实时（目标 150 ms） | 近 30 天：UTC、Berlin 300 RPS 下 P99 106 / 56 ms ✅（优化前拐点 200 RPS）；Kathmandu 200 RPS ✅、300 RPS 157 ms ❌ |
| 缓存命中（目标 20 ms） | 1,500 RPS 下 P99 ≤ 12 ms ✅，2 / 4 个 worker 无差别；更高档位压测机的端口转发先撑不住 |
| 万级推算 | 1 万 QPS、命中率 90%：4 台 4 vCPU API + 2 台 4 vCPU ClickHouse；命中率 11%：12 + 12 台（[design.md §7.4](docs/design.md)） |
| 缓存命中率（P6 任意区间，200 RPS × 120 s，从空缓存开始） | 只有整响应一层时 11.2%、回源率 0.947；加月块后第一层 11.1% + 月块全命中 85.8% + 部分命中 3.0%，**不查库 96.9%、回源率 0.061**（剩下的回源几乎全是含当天的请求现查未封口的日）；20 个热点 key 99.8% |
| 月块命中路径 | 100 个虚拟用户闭环：第一层命中 4,853 QPS / P99 43 ms；任意已封口区间 2,897 QPS / 月块响应 P99 74 ms，每请求 API CPU 约为第一层命中的 2 倍；Redis 每块约 200 B，一个品类一个版本 6 时区 3 平台一年 216 块 ≈ 45 KB |

## 架构

```mermaid
flowchart LR
  subgraph 写入
    X[xlsx / 各平台采集] --> P[解析<br/>按平台源时区转 UTC]
    P --> K[event_key<br/>七字段 sha256]
    K --> Q[质量校验]
    Q --> D[去重<br/>批内 + 跨批]
  end
  D -->|全部行| RAW[(raw_sales_events)]
  D -->|干净行| FACT[(fact_sales)]
  RAW --> DIM[(dim_product)]
  DIM --> CLS[规则引擎<br/>yaml · 多版本]
  CLS --> MEM[(product_class_member)]
  FACT --> RU[汇总重算 · 幂等]
  MEM --> RU
  RU --> H[(agg_product_15m<br/>商品×平台×UTC 15分钟<br/>重算中间层)]
  H --> CH[(agg_class_15m<br/>品类×版本×平台×UTC 15分钟)]
  RU --> DC[(agg_class_daily<br/>预设时区日表)]
  subgraph 读取
    API[FastAPI] --> C{Redis 第一层<br/>整个响应}
    C -->|未命中| B{Redis 第二层<br/>月块：平台 × 本地月}
    B -->|缺块 · 未封口的日| QP[查询规划器]
    QP -->|A 已封口 · 预设时区| DC
    QP -->|B 已封口 · 其余时区 · 当天封口线之前| CH
    QP -->|C 封口线之后的尾巴| FACT
  end
```

**三个关键设计**：

1. **去重必须在聚合之前**。ClickHouse 的去重是最终一致的，物化视图会在插入时就把重复行聚合进去，事后撤不回来。所以去重在写入管线里做完，表引擎只做兜底。
2. **汇总分两层物化，15 分钟粒度**。「商品 × 平台 × UTC 15 分钟」管灵活性：品类口径会变、商品销量不会变，改规则不重算它和明细，它只当重算中间层；「品类 × 规则版本 × 平台 × UTC 15 分钟」与预设时区的品类日表管热路径，每个规则版本一年约 7 万行。现行时区的偏移都是 15 分钟的整数倍，日界在查询时按任意时区切，+05:45 也不用扫明细。
3. **查询规划器分三段读，一天之内也切**：封口线之前读汇总，只有封口线之后的尾巴读明细（明细按 event_ts 打头，尾巴只读几万行），并且把时区纳入分段条件。响应 `meta.plan` 回显每段读了哪张表与精确的 UTC 区间。
4. **两层缓存**：第一层按整个响应，热点请求一次 GET；第一层未命中时从「品类 × 规则版本 × data_gen × 时区 × 单个平台 × 本地月」的月块拼出逐日值，只有缺块和未封口的日查 ClickHouse，再交给同一个 assemble 出周 / 月汇总，口径同源。月块只放已封口的日，靠 key 里的 data_gen 失效。`meta.cache` = hit / block / block_partial / miss / stale。拼装结果与直接计算的逐字段对拍是这一层的准入测试。

完整说明（每个选型的「为什么 / 反方 / 何时换」）见 **[docs/design.md](docs/design.md)**。

## 接口

```
GET /v1/product-classes/{class_slug}/sales-trend
    ?start_date=2024-01-01&end_date=2024-03-31   # 闭区间，最多 366 天
    &platforms=app,website                       # 可选，默认全部
    &tz=Asia/Shanghai                            # IANA 名，默认 UTC
    &granularity=day                             # day | week | month
    &rule_version=v1                             # 可选，默认当前版本
```

```json
{
  "data": {
    "dates":  ["2024-01-01", "2024-01-02", "2024-01-03", "…", "2024-03-31"],
    "series": [
      {"platform": "app",         "values": [10, 13, 0, "…", 0,    0,    null]},
      {"platform": "marketplace", "values": [0,  0,  7, "…", null, null, null]},
      {"platform": "website",     "values": [19, 0,  0, "…", 6,    null, null]}
    ],
    "total": [29, 13, 7, "…", 6, 0, null]
  },
  "meta": {
    "tz": "UTC", "rule_version": "v1", "granularity": "day",
    "data_through": "2024-03-13T09:45:00Z",
    "data_through_by_platform": {"app": "2024-03-13T09:45:00Z", "marketplace": "2024-03-11T15:15:00Z",
                                 "website": "2024-03-12T18:05:00Z"},
    "partial_dates": ["2024-03-11", "2024-03-12", "2024-03-13"],
    "source_tz_assumed": true,
    "warnings": [],
    "sum": {"app": 368, "marketplace": 372, "website": 377, "all": 1117},
    "plan": [
      {"segment": "class_daily", "table": "agg_class_daily", "from": "2024-01-01", "to": "2024-03-12",
       "utc_from": "2024-01-01T00:00:00Z", "utc_to": "2024-03-13T00:00:00Z"},
      {"segment": "class_15m",   "table": "agg_class_15m",   "from": "2024-03-13", "to": "2024-03-13",
       "utc_from": "2024-03-13T00:00:00Z", "utc_to": "2024-03-13T09:45:00Z"},
      {"segment": "fact",        "table": "fact_sales",      "from": "2024-03-13", "to": "2024-03-13",
       "utc_from": "2024-03-13T09:45:00Z", "utc_to": "2024-03-14T00:00:00Z"}
    ],
    "cache": "miss", "stale": false
  }
}
```

上例省略号后三列是 03-12、03-13、03-31。字段口径：

| 字段 | 含义 |
|---|---|
| `values` 里的 `0` / `null` | `0` = 没卖出去；`null` = 晚于**该平台**的数据截止，还不知道 |
| `total` | 选中平台全部为 null → null；部分为 null → 非 null 平台之和，并把这一天记入 `partial_dates` |
| `meta.data_through_by_platform` | 各平台截止时间，**从该平台最新事件推断**。生产应换成采集侧心跳与接收水位：平台某天真的零销量时，推断值会把它误判成 null |
| `meta.partial_dates` | 不完整的本地日：含某个平台的截止时刻，或部分平台已为 null |
| `meta.source_tz_assumed` | 选中平台里有源时区**未与采集方确认**的（样例三个平台都是） |
| `meta.warnings` | 不影响返回的提示，没有时为 `[]`。请求 `tz` 与未确认的源时区不同时带 `{"code": "SOURCE_TZ_UNCONFIRMED", "message", "platforms", "source_tz"}`：换算可能引入整体偏移 |

`granularity=week|month` 时，`data` 下再加四个与 `dates` 平行的数组（仍是列式；`day` 粒度不带）：

| 字段 | 含义 |
|---|---|
| `days_in_bucket` | 桶本身的天数：周 7，月 28–31，不受查询区间裁剪 |
| `days_covered` | 桶内落在查询区间、且不晚于数据截止的天数。「数据截止」取选中平台里最晚的一个；截止当天虽不完整，也算一整天 |
| `partial` | `days_covered < days_in_bucket`，或桶内含 `partial_dates` 里的日 |
| `daily_avg` | `total / days_covered`，保留一位小数；`days_covered` 为 0 时为 null。只给 total 的日均，不给按平台的日均：各平台截止不同，按平台日均要用各自的覆盖天数（例如 marketplace 3 月只有 11 天），需要时用 `data_through_by_platform` 自行折算 |

```json
"data": {"dates": ["2024-01-01", "2024-02-01", "2024-03-01"], "total": [484, 445, 188],
         "days_in_bucket": [31, 29, 31], "days_covered": [31, 29, 13],
         "partial": [false, false, true], "daily_avg": [15.6, 15.3, 14.5], "series": ["…"]}
```

错误体遵循 RFC 9457（`application/problem+json`），11 个错误码，每个都有测试：

```json
{"type": "https://salestrend.example.com/problems/invalid-tz", "title": "Invalid time zone", "status": 400,
 "detail": "tz: '+08:00' is not an IANA time zone name, e.g. Asia/Shanghai", "code": "INVALID_TZ",
 "invalid_params": [{"name": "tz", "reason": "'+08:00' is not an IANA time zone name, e.g. Asia/Shanghai"}]}
```

完整定义见 [docs/openapi.json](docs/openapi.json)（`TrendResponse` schema 带逐字段说明）；`make demo` 打印 10 条真实请求与响应。

## 交付物对照

| 题面要求 | 位置 |
|---|---|
| maxi dress 识别 / 归一化规则 | [rules/maxi_dress.v1.yaml](rules/maxi_dress.v1.yaml) · [design.md §3.3](docs/design.md) · v3 影子版本（中心词 + 长度槽分档，32 条对抗用例）见 [docs/classification-v3.md](docs/classification-v3.md) |
| 存储方案、schema、索引 | [sql/ddl/001_tables.sql](sql/ddl/001_tables.sql) · [design.md §1–2](docs/design.md) · ES 等价方案 [es/](es/README.md) |
| 核心查询（按日期、平台分组） | [sql/queries/](sql/queries/) 三段 · [sql/rollup/](sql/rollup/) 汇总重算 |
| RESTful API 与错误处理 | [src/salestrend/api/](src/salestrend/api/) · [docs/openapi.json](docs/openapi.json) |
| 大数据量与高并发 | [design.md §4–7](docs/design.md) · [压测报告](docs/perf-report.md) |
| 重复、标题归一化、时区、异常记录 | [design.md §3](docs/design.md) · [数据质量报告](docs/data-quality-report.md)（8 行探针逐条结论） |

## 代码结构

```
src/salestrend/
├── ingest/pipeline.py     解析、源时区、event_key、质量校验、去重（纯函数）
├── classify/rules.py      标题归一化、规则引擎、多版本
├── query/planner.py       三段查询规划，按 UTC 半开区间切到一天之内（纯函数）
├── store.py               ClickHouse：建表、写入、汇总重算、三段查询
├── service.py             整响应缓存 → singleflight → 规划 → 月块拼装 / 执行 → 补零 → 降级
├── api/                   FastAPI 路由、参数校验、RFC 9457 错误体
├── cache/resilience.py    Redis 整响应缓存、singleflight（按 key / 按块）、令牌桶、熔断
├── cache/blocks.py        第二层月块：key、只放已封口日、失效校验、MGET / 写回
└── report/quality.py      数据质量报告
tests/unit                 归一化、规则、管线、规划器、11 个错误码、缓存与降级、月块对拍（假存储，不需要数据库）
tests/integration          真 ClickHouse 黄金值、四条路径对拍（含 +05:45 / +08:45、v2 / v3、当天切开）、月块对拍（真 Redis）、重算幂等
```

## 已知局限与下一步

- **源时区未确认**：三个平台暂按 UTC 解析（`config/platforms.yaml` 里 `confirmed: false`），等于按时间戳上写的日期原样切日。请求别的 `tz` 时响应带 `SOURCE_TZ_UNCONFIRMED` 警告，不拒绝。确认后改配置并重放；采集契约应要求时间戳带偏移量（见 [design.md §3.1](docs/design.md)）。
- **各平台截止时间是推断的**：已按各平台自己的截止时间判 null（marketplace 03-12 起、website 03-13 起为 null），但截止时间取自该平台最新事件。平台某天真的零销量时会被误判成 null；生产要靠采集侧心跳与接收水位。
- **实时路径的拐点最低**（P5 实测，单机 4 vCPU）：UTC、Berlin 的已聚合请求 300 RPS 内 P99 ≤ 27 ms（Kathmandu 已聚合在 200 RPS 一档 115 ms，判断为单轮波动，照实记为不达标）；含当天的请求每次要多跑一段明细，UTC、Berlin 到 300 RPS 达标、Kathmandu 300 RPS 157 ms 略超 150 ms 目标。v1.2.0 的「小时表段不扛并发」已解决：根因是商品小时表全表扫描（FINAL 只在 part 未合并时放大代价，合并后可忽略），B 段已改读品类 15 分钟表。见[压测报告](docs/perf-report.md) P5 与 [design.md §2](docs/design.md)。
- **单机压测有上限**：k6、API、ClickHouse 在同一台机器上；命中路径到 2,000 RPS 时 colima 的端口转发先垮了，所以命中路径只测到 1,500 RPS，不是服务拐点。万级推算基于单机实测的每请求 CPU，是量级而不是承诺。
- **明细段的 31 天上限与单独限流**：v1.2.0 注释里写了但并未落实；改成 15 分钟粒度后明细只剩封口线之后的尾巴，这两条已不再需要（[design.md §5](docs/design.md)）。剩下的风险是重算停摆让尾巴变长，应监控 `now − sealed_until`。
- **singleflight 与限流是进程内的**：多实例部署时，singleflight 锁（整响应与月块两套）与令牌桶应放到 Redis 或网关。
- **月块路径吃 API CPU**：任意区间下 86% 的请求由月块拼出，每个约是第一层命中的 2 倍 CPU（100 VU 实测 0.42 vs 0.21 ms·核）；ClickHouse 不再随区间随机程度增长，压力转到 API 上（[design.md §7.0、§7.4](docs/design.md)）。
- **重算任务没有接调度器**：演示用 `make seed` 一次性重算；生产按「每 5 分钟最近 3 小时、每天最近 2 天」接 cron 或调度系统。
- **没有鉴权**：只按 `X-API-Key` 头做限流分桶演示。
