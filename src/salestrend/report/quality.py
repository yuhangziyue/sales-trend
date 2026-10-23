"""数据质量报告：只依赖写入管线的纯函数，不需要数据库，`make report` 随时可跑。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..classify.rules import RuleSet, normalize
from ..ingest.pipeline import Event, clean

# 出题方埋的 8 行探针（user_id 以 U2 开头），逐条给结论
PROBE_NOTES = {
    "U2091": "跨月边界：01-31 23:55。按 UTC 记 1 月；按 Asia/Shanghai 出报表落到 02-01。由 tz 参数决定，不在入库时写死",
    "U2092": "跨月边界：02-01 00:05。源时区若实为 +08:00，UTC 下应属 01-31。源时区按平台配置，默认 UTC",
    "U2093": "闰日边界：2024-02-29 23:50。Date 原生类型处理闰日；按 Asia/Shanghai 落到 03-01",
    "U2094": "跨月边界：03-01 00:10。源时区若实为 +08:00，UTC 下应属 02-29",
    "U2095": "标题插词变体 'Floral long maxi dress'（2005）。词集合含 maxi 与 dress，命中 R1；long 不改判",
    "U2096": "标题词序变体 'Denim Maxi Dress' / 'Maxi Denim Dress'（2008）。LIKE '%maxi dress%' 会漏，词集合判定不漏",
    "U2097": "所有格变体 'Womens Maxi Dress Floral'（2010）。去所有格后与 \"Women's Maxi-Dress Floral\" 归一到同一写法",
    "U2098": "边界品类 'Elegant Long Dress'（2017）。只写 long 没写 maxi，v1 判 review 不计入；同时是全量数据的最晚一条",
}


def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report(events: list[Event], rulesets: dict[tuple[str, str], RuleSet], source_file: str,
                 confirmed: dict[str, bool] | None = None) -> str:
    """confirmed：平台 → 源时区是否已与采集方确认；不传时读 config/platforms.yaml。"""
    if confirmed is None:
        from ..settings import get_settings

        confirmed = get_settings().source_tz_confirmed
    good = clean(events)
    rejected = [e for e in events if e.rejected]
    dups = [e for e in events if e.is_duplicate]
    first_row = {}
    for e in events:
        first_row.setdefault(e.event_key, e.row_no)

    titles: dict[int, Counter] = defaultdict(Counter)
    cats: dict[int, Counter] = defaultdict(Counter)
    for e in good:
        titles[e.product_id][e.title_raw] += 1
        cats[e.product_id][e.category_raw] += 1

    parts = [
        "# 数据质量报告",
        "",
        f"> 由 `make report` 自动生成 · 数据源 `{source_file}` · 生成于 "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## 1 · 总览",
        "",
        _md_table(["指标", "值"], [
            ["原始行数", len(events)],
            ["拒收行（只进 raw）", len(rejected)],
            ["重复行（只进 raw，标记 dup_of）", len(dups)],
            ["进入 fact 的干净行", len(good)],
            ["product_id 数", len(titles)],
            ["数据时间范围（按源时区解析后的 UTC）",
             f"{min(e.event_ts for e in good):%Y-%m-%d %H:%M} → {max(e.event_ts for e in good):%Y-%m-%d %H:%M}"],
        ]),
        "",
        "处理口径：**只标不删**。raw 表保留全部原始行，审计时能回答「这个数为什么少了」；"
        "fact 只进干净行。去重键为七个原始字段规范化后的 sha256，标题不做归一化（不同写法是不同上报）。",
        "",
        "## 2 · 重复记录",
        "",
        _md_table(["行号", "重复于行", "product_id", "title", "sales", "timestamp", "source"], [
            [e.row_no, first_row[e.event_key], e.product_id, e.title_raw, e.sales_count, e.ts_raw, e.source]
            for e in dups]) if dups else "无。",
        "",
        "## 3 · 拒收记录",
        "",
        _md_table(["行号", "原因", "原始值"], [[e.row_no, ",".join(e.dq_flags), e.title_raw] for e in rejected])
        if rejected else "无。校验规则仍然生效：product_id 非整数、时间无法解析、销量为负、未登记平台均拒收，见单测。",
        "",
        "## 4 · 标题变体（同一 product_id 的不同写法）",
        "",
        _md_table(["product_id", "写法", "归一化后"], [
            [pid, " / ".join(f"`{t}`" for t in sorted(ts)), " / ".join(sorted({normalize(t) for t in ts}))]
            for pid, ts in sorted(titles.items()) if len(ts) > 1]),
        "",
    ]

    for i, ((slug, ver), rs) in enumerate(sorted(rulesets.items()), 1):
        rows = []
        for pid in sorted(titles):
            d = rs.classify_product(sorted(titles[pid]), cats[pid].most_common(1)[0][0])
            sales = sum(e.sales_count for e in good if e.product_id == pid)
            if d.decision != "exclude" or d.warnings or "maxi" in normalize(" ".join(titles[pid])):
                rows.append([pid, " / ".join(sorted(titles[pid])), cats[pid].most_common(1)[0][0],
                             d.decision, d.rule_id, ",".join(d.warnings) or "—", sales])
        matched = [r for r in rows if r[3] == "match"]
        parts += [
            f"## 5.{i} · 品类判定 `{slug}` 规则 {ver}{'（默认）' if rs.default else '（影子版本）'}",
            "",
            f"命中 {len(matched)} 个商品，合计 {sum(r[6] for r in matched)} 件。下表只列命中、待审、告警，"
            "以及标题含 maxi 但被排除的商品。",
            "",
            _md_table(["product_id", "标题写法", "原始类目", "判定", "命中规则", "告警", "去重后销量"], rows),
            "",
        ]

    sources = sorted({e.source for e in good})
    unconfirmed = [s for s in sources if not confirmed.get(s, False)]
    sh = ZoneInfo("Asia/Shanghai")
    boundary = [e for e in good if "day_boundary" in e.dq_flags]
    parts += [
        "## 6 · 日界敏感行（换时区会换日）",
        "",
        _md_table(["行号", "user_id", "原始时间", "按 UTC 归属", "按 Asia/Shanghai 归属", "product_id", "sales"], [
            [e.row_no, e.user_id, e.ts_raw, e.event_ts.date(), e.event_ts.astimezone(sh).date(), e.product_id,
             e.sales_count] for e in boundary]),
        "",
        "## 7 · 出题方探针（user_id 以 U2 开头）逐条结论",
        "",
        _md_table(["user_id", "行号", "原始时间", "标题", "结论"], [
            [e.user_id, e.row_no, e.ts_raw, e.title_raw, PROBE_NOTES.get(e.user_id, "—")]
            for e in events if e.user_id.startswith("U2")]),
        "",
        "## 8 · 各平台数据截止时间",
        "",
        _md_table(["平台", "源时区", "已与采集方确认", "最新事件（UTC）", "行数"], [
            [src, next(e.source_tz for e in good if e.source == src),
             "是" if confirmed.get(src, False) else "**否**（假设）",
             max(e.event_ts for e in good if e.source == src).strftime("%Y-%m-%d %H:%M"),
             sum(1 for e in good if e.source == src)]
            for src in sources]),
        "",
        "数据只到 2024-03-13：之后的日期 API 返回 `null`（还没数据），与 `0`（没卖出去）区分。",
        "",
        "注意三个平台的最新事件时间不同（marketplace 最晚到 03-11）。当前接口用全局截止时间判 null，"
        "所以 marketplace 在 03-12、03-13 显示为 0。样例数据无法区分「该平台那两天没卖」和「该平台数据还没到」，"
        "生产应由采集侧上报各平台的**接收水位**（不是最新事件时间），再按平台判 null。",
        "",
        "## 9 · 时区假设",
        "",
        "timestamp 不带时区，源时区按平台登记在 `config/platforms.yaml`（`source_tz` + `confirmed`）。",
        "",
        (f"未确认的平台：{'、'.join(f'`{s}`' for s in unconfirmed)}（共 {len(unconfirmed)}/{len(sources)} 个）。"
         "当前 source_tz 是我们的假设：小时分布集中在 07–22 点，更像本地营业时间，**生产环境必须与采集方确认**。"
         "查询时只要请求 tz 与未确认的 source_tz 不同，响应 meta.warnings 会带 `SOURCE_TZ_UNCONFIRMED`。"
         if unconfirmed else "全部平台的源时区均已与采集方确认。"),
        "",
        "确认后改配置（`confirmed: true`）并重放 ingest 即可，raw 表保留了原始时间文本 ts_raw。",
        "",
    ]
    return "\n".join(parts)
