"""命令行：salestrend seed | ingest | rollup | report | demo。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .classify.rules import load_rulesets
from .ingest.pipeline import clean, process, read_xlsx
from .settings import ROOT, get_settings

DEFAULT_XLSX = ROOT / "data" / "sample-sales-data.xlsx"


def _store():
    from .store import Store

    return Store(get_settings())


def cmd_ingest(path: Path, reset: bool) -> None:
    s = get_settings()
    store = _store()
    if reset:
        store.reset()
        print("tables recreated")
    rows = read_xlsx(path)
    events = process(rows, s.platforms)
    known = store.known_keys(events)  # 跨批去重：同一文件导两次，第二次全部标为重复
    if known:
        events = process(rows, s.platforms, known_keys=known)
    batch = store.write_events(events)
    print(f"batch {batch}: raw {len(events)} · fact +{len(clean(events))} · "
          f"duplicates {sum(e.is_duplicate for e in events)} · rejected {sum(e.rejected for e in events)}")
    rulesets = load_rulesets(s.rules_dir)
    store.refresh_dim_product()
    result = store.classify(rulesets)
    for (slug, ver), decisions in sorted(result.items()):
        counts = {k: sum(d.decision == k for d in decisions.values()) for k in ("match", "review", "exclude")}
        print(f"classified {slug} {ver}: {counts}")
    cmd_rollup()
    _bump_data_gen()


def cmd_rollup() -> None:
    s = get_settings()
    r = _store().rollup(load_rulesets(s.rules_dir))
    print(f"rollup: sealed_until {r['sealed_until']:%Y-%m-%d %H:%M}Z · data_through {r['data_through']:%Y-%m-%d %H:%M}Z"
          f" · class_daily jobs {r['class_daily_jobs']} · version {r['version']}")


def _bump_data_gen() -> None:
    from .cache.resilience import ResponseCache

    asyncio.run(ResponseCache(get_settings().redis_url).bump_data_gen())  # 回填后旧缓存 key 自然作废


def cmd_report(path: Path, out: Path) -> None:
    from .report.quality import build_report

    s = get_settings()
    events = process(read_xlsx(path), s.platforms)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(events, load_rulesets(s.rules_dir), path.name))
    print(f"wrote {out}")


def cmd_openapi(out: Path) -> None:
    from .api.app import create_app

    out.write_text(json.dumps(create_app(use_redis=False).openapi(), ensure_ascii=False, indent=2))
    print(f"wrote {out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="salestrend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed", help="重建表 + 导入 xlsx + 分类 + 汇总重算")
    p.add_argument("--file", type=Path, default=DEFAULT_XLSX)
    p = sub.add_parser("ingest", help="增量导入（不重建表），演示跨批去重")
    p.add_argument("--file", type=Path, default=DEFAULT_XLSX)
    sub.add_parser("rollup", help="汇总重算（幂等）")
    p = sub.add_parser("report", help="生成数据质量报告（不需要数据库）")
    p.add_argument("--file", type=Path, default=DEFAULT_XLSX)
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "data-quality-report.md")
    p = sub.add_parser("openapi", help="导出 OpenAPI")
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "openapi.json")
    a = ap.parse_args(argv)

    if a.cmd == "seed":
        cmd_ingest(a.file, reset=True)
    elif a.cmd == "ingest":
        cmd_ingest(a.file, reset=False)
    elif a.cmd == "rollup":
        cmd_rollup()
    elif a.cmd == "report":
        cmd_report(a.file, a.out)
    elif a.cmd == "openapi":
        cmd_openapi(a.out)


if __name__ == "__main__":
    sys.exit(main())
