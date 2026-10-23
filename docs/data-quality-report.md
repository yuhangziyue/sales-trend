# 数据质量报告

> 由 `make report` 自动生成 · 数据源 `sample-sales-data.xlsx` · 生成于 2026-10-23 03:09 UTC

## 1 · 总览

| 指标 | 值 |
|---|---|
| 原始行数 | 300 |
| 拒收行（只进 raw） | 0 |
| 重复行（只进 raw，标记 dup_of） | 12 |
| 进入 fact 的干净行 | 288 |
| product_id 数 | 30 |
| 数据时间范围（按源时区解析后的 UTC） | 2024-01-01 07:00 → 2024-03-13 09:45 |

处理口径：**只标不删**。raw 表保留全部原始行，审计时能回答「这个数为什么少了」；fact 只进干净行。去重键为七个原始字段规范化后的 sha256，标题不做归一化（不同写法是不同上报）。

## 2 · 重复记录

| 行号 | 重复于行 | product_id | title | sales | timestamp | source |
|---|---|---|---|---|---|---|
| 47 | 46 | 2007 | Formal Maxi Dress | 2 | 2024-01-11 21:14:00 | app |
| 71 | 70 | 2020 | Oxford Shirt | 10 | 2024-01-17 14:55:00 | website |
| 93 | 92 | 2020 | Oxford Shirt | 18 | 2024-01-23 10:59:00 | marketplace |
| 111 | 110 | 2026 | Wide Leg Pants | 8 | 2024-01-27 12:09:00 | app |
| 153 | 152 | 2024 | Pants - Casual | 5 | 2024-02-05 19:52:00 | website |
| 162 | 161 | 2011 | MAXI DRESS WHITE | 18 | 2024-02-07 19:56:00 | app |
| 164 | 163 | 2023 | White Sneakers | 15 | 2024-02-08 08:53:00 | marketplace |
| 180 | 179 | 2025 | Pants - Formal | 14 | 2024-02-11 15:56:00 | marketplace |
| 198 | 197 | 2014 | Summer Mini Dress | 12 | 2024-02-16 08:05:00 | website |
| 214 | 213 | 2029 | Long Floral Skirt | 13 | 2024-02-19 22:07:00 | website |
| 222 | 221 | 2021 | Running Shoes | 5 | 2024-02-21 20:37:00 | marketplace |
| 265 | 264 | 2027 | Maxi Skirt | 3 | 2024-03-02 18:51:00 | app |

## 3 · 拒收记录

无。校验规则仍然生效：product_id 非整数、时间无法解析、销量为负、未登记平台均拒收，见单测。

## 4 · 标题变体（同一 product_id 的不同写法）

| product_id | 写法 | 归一化后 |
|---|---|---|
| 2001 | `Maxi Dress Floral` / `maxi dress floral` | maxi dress floral |
| 2002 | `SUMMER MAXI DRESS` / `Summer Maxi Dress` | summer maxi dress |
| 2003 | `Red Maxi Dress` / `Red Maxi-Dress` | red maxi dress |
| 2004 | `Blue Maxi Dress` / `blue maxi  dress` | blue maxi dress |
| 2005 | `Floral Maxi Dress` / `Floral long maxi dress` | floral long maxi dress / floral maxi dress |
| 2008 | `Denim Maxi Dress` / `Maxi Denim Dress` | denim maxi dress / maxi denim dress |
| 2010 | `Women's Maxi-Dress Floral` / `Womens Maxi Dress Floral` | women maxi dress floral |

## 5.1 · 品类判定 `maxi-dress` 规则 v1（默认）

命中 12 个商品，合计 1117 件。下表只列命中、待审、告警，以及标题含 maxi 但被排除的商品。

| product_id | 标题写法 | 原始类目 | 判定 | 命中规则 | 告警 | 去重后销量 |
|---|---|---|---|---|---|---|
| 2001 | Maxi Dress Floral / maxi dress floral | dress | match | R1_maxi_and_dress | — | 118 |
| 2002 | SUMMER MAXI DRESS / Summer Maxi Dress | dress | match | R1_maxi_and_dress | — | 94 |
| 2003 | Red Maxi Dress / Red Maxi-Dress | dress | match | R1_maxi_and_dress | — | 108 |
| 2004 | Blue Maxi Dress / blue maxi  dress | dress | match | R1_maxi_and_dress | — | 90 |
| 2005 | Floral Maxi Dress / Floral long maxi dress | dress | match | R1_maxi_and_dress | — | 106 |
| 2006 | Black Maxi Dress | dress | match | R1_maxi_and_dress | — | 56 |
| 2007 | Formal Maxi Dress | dress | match | R1_maxi_and_dress | — | 89 |
| 2008 | Denim Maxi Dress / Maxi Denim Dress | dress | match | R1_maxi_and_dress | — | 126 |
| 2009 | Long Maxi Summer Dress | dress | match | R1_maxi_and_dress | — | 83 |
| 2010 | Women's Maxi-Dress Floral / Womens Maxi Dress Floral | dress | match | R1_maxi_and_dress | — | 105 |
| 2011 | MAXI DRESS WHITE | dress | match | R1_maxi_and_dress | — | 96 |
| 2012 | Maxi Floral Evening Dress | dress | match | R1_maxi_and_dress | — | 46 |
| 2017 | Elegant Long Dress | dress | review | R3_long_ambiguous | — | 100 |
| 2018 | T-shirt Dress | shirt | exclude | R0_default | C1_category_conflict | 54 |
| 2027 | Maxi Skirt | skirt | exclude | R2_other_garment | — | 98 |

## 5.2 · 品类判定 `maxi-dress` 规则 v2（影子版本）

命中 13 个商品，合计 1217 件。下表只列命中、待审、告警，以及标题含 maxi 但被排除的商品。

| product_id | 标题写法 | 原始类目 | 判定 | 命中规则 | 告警 | 去重后销量 |
|---|---|---|---|---|---|---|
| 2001 | Maxi Dress Floral / maxi dress floral | dress | match | R1_maxi_and_dress | — | 118 |
| 2002 | SUMMER MAXI DRESS / Summer Maxi Dress | dress | match | R1_maxi_and_dress | — | 94 |
| 2003 | Red Maxi Dress / Red Maxi-Dress | dress | match | R1_maxi_and_dress | — | 108 |
| 2004 | Blue Maxi Dress / blue maxi  dress | dress | match | R1_maxi_and_dress | — | 90 |
| 2005 | Floral Maxi Dress / Floral long maxi dress | dress | match | R1_maxi_and_dress | — | 106 |
| 2006 | Black Maxi Dress | dress | match | R1_maxi_and_dress | — | 56 |
| 2007 | Formal Maxi Dress | dress | match | R1_maxi_and_dress | — | 89 |
| 2008 | Denim Maxi Dress / Maxi Denim Dress | dress | match | R1_maxi_and_dress | — | 126 |
| 2009 | Long Maxi Summer Dress | dress | match | R1_maxi_and_dress | — | 83 |
| 2010 | Women's Maxi-Dress Floral / Womens Maxi Dress Floral | dress | match | R1_maxi_and_dress | — | 105 |
| 2011 | MAXI DRESS WHITE | dress | match | R1_maxi_and_dress | — | 96 |
| 2012 | Maxi Floral Evening Dress | dress | match | R1_maxi_and_dress | — | 46 |
| 2017 | Elegant Long Dress | dress | match | R3_long_as_maxi | — | 100 |
| 2018 | T-shirt Dress | shirt | exclude | R0_default | C1_category_conflict | 54 |
| 2027 | Maxi Skirt | skirt | exclude | R2_other_garment | — | 98 |

## 5.3 · 品类判定 `maxi-dress` 规则 v3（影子版本）

命中 12 个商品，合计 1117 件。下表只列命中、待审、告警，以及标题含 maxi 但被排除的商品。

| product_id | 标题写法 | 原始类目 | 判定 | 命中规则 | 告警 | 去重后销量 |
|---|---|---|---|---|---|---|
| 2001 | Maxi Dress Floral / maxi dress floral | dress | match | S1_strong | — | 118 |
| 2002 | SUMMER MAXI DRESS / Summer Maxi Dress | dress | match | S1_strong | — | 94 |
| 2003 | Red Maxi Dress / Red Maxi-Dress | dress | match | S1_strong | — | 108 |
| 2004 | Blue Maxi Dress / blue maxi  dress | dress | match | S1_strong | — | 90 |
| 2005 | Floral Maxi Dress / Floral long maxi dress | dress | match | S1_strong | — | 106 |
| 2006 | Black Maxi Dress | dress | match | S1_strong | — | 56 |
| 2007 | Formal Maxi Dress | dress | match | S1_strong | — | 89 |
| 2008 | Denim Maxi Dress / Maxi Denim Dress | dress | match | S1_strong | — | 126 |
| 2009 | Long Maxi Summer Dress | dress | match | S1_strong | — | 83 |
| 2010 | Women's Maxi-Dress Floral / Womens Maxi Dress Floral | dress | match | S1_strong | — | 105 |
| 2011 | MAXI DRESS WHITE | dress | match | S1_strong | — | 96 |
| 2012 | Maxi Floral Evening Dress | dress | match | S1_strong | — | 46 |
| 2017 | Elegant Long Dress | dress | review | S3_weak_only | — | 100 |
| 2018 | T-shirt Dress | shirt | exclude | S0_no_length | C1_category_conflict | 54 |
| 2027 | Maxi Skirt | skirt | exclude | H1_head_other_garment | — | 98 |

## 6 · 日界敏感行（换时区会换日）

| 行号 | user_id | 原始时间 | 按 UTC 归属 | 按 Asia/Shanghai 归属 | product_id | sales |
|---|---|---|---|---|---|---|
| 131 | U2091 | 2024-01-31 23:55:00 | 2024-01-31 | 2024-02-01 | 2001 | 6 |
| 132 | U2092 | 2024-02-01 00:05:00 | 2024-02-01 | 2024-02-01 | 2002 | 7 |
| 255 | U2093 | 2024-02-29 23:50:00 | 2024-02-29 | 2024-03-01 | 2003 | 5 |
| 256 | U2094 | 2024-03-01 00:10:00 | 2024-03-01 | 2024-03-01 | 2004 | 8 |

## 7 · 出题方探针（user_id 以 U2 开头）逐条结论

| user_id | 行号 | 原始时间 | 标题 | 结论 |
|---|---|---|---|---|
| U2091 | 131 | 2024-01-31 23:55:00 | maxi dress floral | 跨月边界：01-31 23:55。按 UTC 记 1 月；按 Asia/Shanghai 出报表落到 02-01。由 tz 参数决定，不在入库时写死 |
| U2092 | 132 | 2024-02-01 00:05:00 | SUMMER MAXI DRESS | 跨月边界：02-01 00:05。源时区若实为 +08:00，UTC 下应属 01-31。源时区按平台配置，默认 UTC |
| U2093 | 255 | 2024-02-29 23:50:00 | Red Maxi-Dress | 闰日边界：2024-02-29 23:50。Date 原生类型处理闰日；按 Asia/Shanghai 落到 03-01 |
| U2094 | 256 | 2024-03-01 00:10:00 | blue maxi  dress | 跨月边界：03-01 00:10。源时区若实为 +08:00，UTC 下应属 02-29 |
| U2095 | 297 | 2024-03-10 12:30:00 | Floral long maxi dress | 标题插词变体 'Floral long maxi dress'（2005）。词集合含 maxi 与 dress，命中 R1；long 不改判 |
| U2096 | 299 | 2024-03-11 15:15:00 | Denim Maxi Dress | 标题词序变体 'Denim Maxi Dress' / 'Maxi Denim Dress'（2008）。LIKE '%maxi dress%' 会漏，词集合判定不漏 |
| U2097 | 300 | 2024-03-12 18:05:00 | Womens Maxi Dress Floral | 所有格变体 'Womens Maxi Dress Floral'（2010）。去所有格后与 "Women's Maxi-Dress Floral" 归一到同一写法 |
| U2098 | 301 | 2024-03-13 09:45:00 | Elegant Long Dress | 边界品类 'Elegant Long Dress'（2017）。只写 long 没写 maxi，v1 判 review 不计入；同时是全量数据的最晚一条 |

## 8 · 各平台数据截止时间

| 平台 | 源时区 | 已与采集方确认 | 最新事件（UTC） | 行数 |
|---|---|---|---|---|
| app | UTC | **否**（假设） | 2024-03-13 09:45 | 97 |
| marketplace | UTC | **否**（假设） | 2024-03-11 15:15 | 95 |
| website | UTC | **否**（假设） | 2024-03-12 18:05 | 96 |

数据只到 2024-03-13：之后的日期 API 返回 `null`（还没数据），与 `0`（没卖出去）区分。

注意三个平台的最新事件时间不同（marketplace 最晚到 03-11、website 到 03-12）。接口按平台各自的截止时间判 null：marketplace 从 03-12 起、website 从 03-13 起为 null，响应 `meta.data_through_by_platform` 给出各平台截止。这个截止是从最新事件**推断**的，样例数据无法区分「该平台那两天没卖」和「该平台数据还没到」，生产应由采集侧上报各平台的**接收水位**（不是最新事件时间）取代它。

## 9 · 时区假设

timestamp 不带时区，源时区按平台登记在 `config/platforms.yaml`（`source_tz` + `confirmed`）。

未确认的平台：`app`、`marketplace`、`website`（共 3/3 个）。当前 source_tz 是我们的假设：小时分布集中在 07–22 点，更像本地营业时间，**生产环境必须与采集方确认**。查询时只要请求 tz 与未确认的 source_tz 不同，响应 meta.warnings 会带 `SOURCE_TZ_UNCONFIRMED`。

确认后改配置（`confirmed: true`）并重放 ingest 即可，raw 表保留了原始时间文本 ts_raw。
