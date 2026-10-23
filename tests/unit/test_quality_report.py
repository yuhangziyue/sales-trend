"""数据质量报告：总览数字、规则版本全量出现、源时区 confirmed 状态可见。"""

from salestrend.report.quality import build_report


def _section(md: str, title_prefix: str) -> str:
    start = md.index(title_prefix)
    nxt = md.find("\n## ", start + 1)
    return md[start: nxt if nxt != -1 else len(md)]


def test_overview_numbers(events, rulesets):
    md = build_report(events, rulesets, "sample-sales-data.xlsx")
    overview = _section(md, "## 1 ·")
    assert "| 原始行数 | 300 |" in overview
    assert "| 重复行（只进 raw，标记 dup_of） | 12 |" in overview
    assert "| 进入 fact 的干净行 | 288 |" in overview


def test_every_ruleset_gets_a_section(events, rulesets):
    md = build_report(events, rulesets, "sample-sales-data.xlsx")
    for slug, ver in rulesets:
        assert f"`{slug}` 规则 {ver}" in md
    assert "命中 12 个商品，合计 1117 件" in _section(md, "## 5.1 ·")


def test_unconfirmed_source_tz_is_flagged(events, rulesets):
    md = build_report(events, rulesets, "x.xlsx",
                      confirmed={"app": True, "marketplace": False, "website": False})
    cutoff = _section(md, "## 8 ·")
    assert "已与采集方确认" in cutoff
    assert "| app | UTC | 是 |" in cutoff
    assert "| marketplace | UTC | **否**（假设） |" in cutoff
    tz = _section(md, "## 9 ·")
    assert "`marketplace`、`website`（共 2/3 个）" in tz


def test_all_confirmed(events, rulesets):
    md = build_report(events, rulesets, "x.xlsx", confirmed=dict.fromkeys(["app", "marketplace", "website"], True))
    assert "全部平台的源时区均已与采集方确认" in _section(md, "## 9 ·")
    assert "**否**" not in _section(md, "## 8 ·")


def test_default_reads_platforms_yaml(events, rulesets, settings):
    md = build_report(events, rulesets, "x.xlsx")
    cutoff = _section(md, "## 8 ·")
    for src, ok in settings.source_tz_confirmed.items():
        assert f"| {src} | {settings.platforms[src]} | {'是' if ok else '**否**（假设）'} |" in cutoff
