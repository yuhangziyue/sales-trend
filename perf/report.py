"""把 k6 --summary-export、/metrics 快照与 perf/p5.py 的结果汇总成 docs/perf-report.md。

报告里「## 结论与解读」之后是手写分析：重新生成时原样保留，只刷新上面由数据生成的表格。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

TARGETS = {
    "hot": ("P1 热缓存", "固定到达率 1000 RPS × 60s，20 个热点 key，预热后", 50),
    "cold": ("P2 冷混合", "固定到达率 200 RPS × 60s，70/25/5 混合，缓存预先清空", 300),
    "herd": ("P3 击穿", "同一冷 key 瞬间 500 并发（每 VU 1 次）", None),
}


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


MARKER = "## 结论与解读"
SLO_P5 = {"class_daily": ("日表段 · 一年 · UTC（预设）", 150), "hourly": ("小时表段 · 一年 · Europe/Berlin", 500),
          "fact": ("明细段 · 31 天 · Asia/Kathmandu", 1500)}


def mb(n) -> str:
    n = int(n)
    return f"{n / 1e6:,.1f} MB" if n >= 1e6 else f"{n / 1e3:,.1f} KB"


def fmt_ms(x) -> str:
    return f"{float(x):.0f}" if float(x) >= 10 else f"{float(x):.1f}"


def p5_section(results: Path) -> list[str]:
    f = results / "p5.json"
    if not f.exists():
        return ["## P5 千万级实测", "", "未运行（`make perf-p5`）。", ""]
    r = json.loads(f.read_text())
    lines = ["## P5 千万级实测", "",
             f"`make perf-p5`：同一个 ClickHouse 里建独立库 `{r['db']}`（同一份 DDL），"
             f"`INSERT … SELECT … FROM numbers({r['rows_requested']:,})` 合成明细：5000 个商品 × 3 个平台，"
             "2024 全年每天 07:00–22:59 均匀分布，销量 1–18；成员表规则版本 `p5` 下 match 500 个商品（每 10 个取 1 个）。"
             "延迟一律取 `system.query_log` 的服务端耗时。", "",
             "### 存储量（system.parts，active parts）", "",
             "| 表 | 行数 | 压缩前 | 压缩后 | 压缩比 | parts |", "|---|---|---|---|---|---|"]
    for t in r["storage"]:
        lines.append(f"| {t['table']} | {int(t['rows']):,} | {mb(t['raw_bytes'])} | {mb(t['bytes'])} | "
                     f"{t['ratio']} | {t['parts']} |")
    if "load" in r:
        lines += ["", "### 写入与重算耗时", "", "| 步骤 | 服务端耗时 | 写入行数 |", "|---|---|---|",
                  f"| 合成明细 + 成员表 | {r['load']['server_ms'] / 1000:.1f} s | {r['load']['written_rows']:,} |",
                  f"| 小时表重算（全年，先删后写） | {r['rollup_hourly']['server_ms'] / 1000:.1f} s | "
                  f"{r['rollup_hourly']['written_rows']:,} |"]
        for tz, v in r["rollup_daily"].items():
            lines.append(f"| 品类日表重算 · {tz}（全年） | {v['server_ms'] / 1000:.2f} s | {v['written_rows']:,} |")
    lines += ["", "### 三段查询（串行 = 同一条 SQL 连跑 N 次；8 并发 = clickhouse-benchmark -c 8）", "",
              "| 段 | FINAL | 串行 n | 串行 P50 / P95 / P99 ms | 8 并发 P50 / P95 / P99 ms | 8 并发 QPS | read_rows | "
              "read_bytes | 峰值内存 | 目标 P99 |",
              "|" + "---|" * 10]
    for name, q in r["queries"].items():
        label, target = SLO_P5[name]
        for k, lab in (("final", "有"), ("nofinal", "去掉")):
            se, c8 = q[f"{k}_serial"], q[f"{k}_c8"]
            ok = ("✅" if se["p99"] <= target else "❌") + " / " + ("✅" if c8["p99"] <= target else "❌")
            lines.append(
                f"| {label} | {lab} | {se['n']} | {fmt_ms(se['p50'])} / {fmt_ms(se['p95'])} / {fmt_ms(se['p99'])} | "
                f"{fmt_ms(c8['p50'])} / {fmt_ms(c8['p95'])} / {fmt_ms(c8['p99'])} | {c8['qps'] or 0:.1f} | "
                f"{se['read_rows']:,} | {mb(se['read_bytes'])} | {mb(se['memory'])} | ≤ {target}（串行 / 并发 {ok}） |")
    lines += ["", "去掉 FINAL 前先比对了结果：" + "；".join(
        f"{SLO_P5[n][0].split(' · ')[0]} {'一致' if q['same_result'] else '**不一致**'}" for n, q in r["queries"].items())
        + "。", "", "### 并发拐点（带 FINAL 的主路径）", "",
        "| 段 | 并发 | 成功 / 失败 | P50 ms | P99 ms | QPS |", "|---|---|---|---|---|---|"]
    for name, q in r["queries"].items():
        pts = sorted([*q["curve"], q["final_c8"]], key=lambda x: x["conc"])
        for c in pts:
            err = c.get("errors", 0)
            fail = f"{err}（code {c['error_code']}）" if err else "0"
            lines.append(f"| {SLO_P5[name][0].split(' · ')[0]} | {c['conc']} | {c['n']} / {fail} | "
                         f"{fmt_ms(c['p50'] or 0)} | {fmt_ms(c['p99'] or 0)} | {c['qps'] or 0:.1f} |")
    e2e = sorted(results.glob("p5_e2e_r*.json"), key=lambda f: int(f.stem.split("_r")[-1]))
    if e2e:
        lines += ["", "### 端到端（指向 p5 库的 API 实例，k6 固定到达率 60s，缓存旁路，一年区间 Europe/Berlin 小时表段）", "",
                  "| 到达率 | 请求数 | P50 ms | P95 ms | P99 ms | 失败率 | 目标 P99 ≤ 500 |", "|---|---|---|---|---|---|---|"]
        for f in e2e:
            m = json.loads(f.read_text())["metrics"]
            d, reqs, failed = m["http_req_duration"], m["http_reqs"], m["http_req_failed"]
            lines.append(f"| {f.stem.split('_r')[-1]} RPS | {reqs['count']} | {d['med']:.0f} | {d['p(95)']:.0f} | "
                         f"{d['p(99)']:.0f} | {failed['value'] * 100:.2f}% | {'✅' if d['p(99)'] <= 500 else '❌'} |")
    return [*lines, ""]


def snapshot(path: Path) -> dict:
    """解析 /metrics 文本，返回 {(name, frozenset(labels)): value}。"""
    from prometheus_client.parser import text_string_to_metric_families

    out = {}
    for fam in text_string_to_metric_families(path.read_text()):
        for smp in fam.samples:
            out[(smp.name, frozenset(smp.labels.items()))] = smp.value
    return out


def hist_quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """与 PromQL histogram_quantile 同样的桶内线性插值。buckets = [(le, 累计数)]。"""
    total = buckets[-1][1] if buckets else 0
    if not total:
        return None
    rank, prev_le, prev_c = q * total, 0.0, 0.0
    for le, c in buckets:
        if c >= rank:
            if le == float("inf"):
                return prev_le
            return prev_le + (le - prev_le) * ((rank - prev_c) / (c - prev_c) if c > prev_c else 0)
        prev_le, prev_c = le, c
    return prev_le


def p6_stats(results: Path, scen: str) -> dict | None:
    b, a, k = (results / f"{scen}_metrics_before.txt", results / f"{scen}_metrics_after.txt", results / f"{scen}.json")
    if not (b.exists() and a.exists() and k.exists()):
        return None
    before, after = snapshot(b), snapshot(a)
    delta = {key: v - before.get(key, 0.0) for key, v in after.items()}

    def total(name, **want):
        return sum(v for (n, lbl), v in delta.items() if n == name and all(dict(lbl).get(x) == y for x, y in want.items()))

    cache = {c: total("trend_requests_total", cache=c) for c in ("hit", "miss", "stale", "none")}
    served = cache["hit"] + cache["miss"] + cache["stale"]
    storage = total("storage_queries_total")
    paths = {}
    for (n, lbl), v in delta.items():
        if n == "trend_request_seconds_count" and v > 0:
            path = dict(lbl)["path"]
            bk = sorted((float(dict(l2)["le"]), c) for (n2, l2), c in delta.items()
                        if n2 == "trend_request_seconds_bucket" and dict(l2)["path"] == path)
            paths[path] = {"n": int(v), "avg_ms": total("trend_request_seconds_sum", path=path) / v * 1000,
                           "p50_ms": (hist_quantile(bk, 0.5) or 0) * 1000, "p99_ms": (hist_quantile(bk, 0.99) or 0) * 1000}
    m = json.loads(k.read_text())["metrics"]
    xc = {c: m.get(f"xcache_{c}", {}).get("count", 0) for c in ("hit", "miss", "stale")}
    return {"cache": cache, "served": served, "hit_rate": cache["hit"] / served if served else 0,
            "storage": storage, "storage_by_seg": {s: total("storage_queries_total", segment=s)
                                                   for s in ("class_daily", "hourly", "fact")},
            "backsource": storage / served if served else 0, "shared": total("singleflight_shared_total"),
            "paths": paths, "k6_reqs": m["http_reqs"]["count"], "k6_xc": xc,
            "k6_dur": m["http_req_duration"], "k6_failed": m["http_req_failed"]["value"]}


def p6_section(results: Path) -> list[str]:
    stats = {s: p6_stats(results, s) for s in ("range", "hot200")}
    if not any(stats.values()):
        return ["## P6 任意区间命中率", "", "未运行（`make perf-p6`）。", ""]
    names = {"range": "P6 任意区间", "hot200": "对照 · 20 个热点 key（P1 的 key 集）"}
    lines = ["## P6 任意区间命中率", "",
             "两组都是固定到达率 200 RPS × 120s、开始前清空 trend 缓存。P6 每个请求随机取：起止日期都落在样例数据范围"
             "（2024-01-01..03-13，73 天）内、长度 1–73 天均匀（题设 1–90 天，但数据只有 73 天）；平台 7 种非空组合；"
             "tz 6 个预设时区；粒度 day 80% · week 10% · month 10%。对照组只打 P1 的 20 个热点 key。"
             "命中率与回源率取压测前后两次 `/metrics` 快照的差值（两个 worker 已由多进程模式汇总），"
             "再用 k6 按 `X-Cache` 响应头计数交叉验证。", "",
             "| 场景 | 请求数（/metrics · k6） | hit / miss / stale | 命中率 | k6 X-Cache 命中率 | 存储查询数 | 回源率 | "
             "singleflight 合并 | 端到端 P50 / P95 / P99 ms | 失败率 |", "|" + "---|" * 10]
    for s, st in stats.items():
        if not st:
            lines.append(f"| {names[s]} | 未运行 |" + " |" * 8)
            continue
        xc = st["k6_xc"]
        k6_hit = xc["hit"] / max(1, sum(xc.values()))
        d = st["k6_dur"]
        lines.append(
            f"| {names[s]} | {st['served']:.0f} · {st['k6_reqs']} | {st['cache']['hit']:.0f} / {st['cache']['miss']:.0f} / "
            f"{st['cache']['stale']:.0f} | **{st['hit_rate'] * 100:.1f}%** | {k6_hit * 100:.1f}% | {st['storage']:.0f} | "
            f"{st['backsource']:.3f} | {st['shared']:.0f} | {d['med']:.1f} / {d['p(95)']:.1f} / {d['p(99)']:.1f} | "
            f"{st['k6_failed'] * 100:.2f}% |")
    lines += ["", "按 path 的服务端延迟（`trend_request_seconds` 直方图差值；P50 / P99 为桶内插值，精度受桶宽限制）：", "",
              "| 场景 | path | 请求数 | 平均 ms | P50 ms | P99 ms |", "|---|---|---|---|---|---|"]
    for s, st in stats.items():
        for path, v in sorted((st or {}).get("paths", {}).items(), key=lambda x: -x[1]["n"]):
            lines.append(f"| {names[s]} | {path} | {v['n']} | {v['avg_ms']:.1f} | {v['p50_ms']:.1f} | {v['p99_ms']:.1f} |")
    st = stats.get("range")
    if st:
        lines += ["", "P6 存储查询按段：" + " · ".join(f"{k} {v:.0f}" for k, v in st["storage_by_seg"].items()) + "。"]
    return [*lines, ""]


def main(results: Path, out: Path) -> None:
    rows = []
    for key, (name, how, p99_target) in TARGETS.items():
        f = results / f"{key}.json"
        if not f.exists():
            rows.append([name, how, "未运行", "", "", "", "", ""])
            continue
        m = json.loads(f.read_text())["metrics"]
        d, reqs, failed = m["http_req_duration"], m["http_reqs"], m["http_req_failed"]
        p99 = d["p(99)"]
        ok = "—" if p99_target is None else ("✅" if p99 <= p99_target else f"❌ 目标 ≤ {p99_target}")
        rows.append([name, how, f'{reqs["count"]}', f'{reqs["rate"]:.0f}', f'{d["med"]:.1f}', f'{d["p(95)"]:.1f}',
                     f"{p99:.1f} {ok}", f'{failed["value"] * 100:.2f}%'])

    deg = results / "degrade.txt"
    deg = dict(ln.split("=", 1) for ln in deg.read_text().split("\n") if "=" in ln) if deg.exists() else {}
    herd_q = (results / "herd_storage_queries.txt")
    herd_q = herd_q.read_text().strip() if herd_q.exists() else "?"
    cpu = sh("sysctl -n machdep.cpu.brand_string") or platform.processor()
    vm = sh("colima list 2>/dev/null | awk 'NR==2{print $4\" CPU / \"$5\" 内存\"}'")

    # 手写的结论段落：从旧报告里原样取回
    manual = None
    if out.exists() and MARKER in (old := out.read_text()):
        manual = [MARKER + old.split(MARKER, 1)[1].rstrip("\n"), ""]

    table = ["| 场景 | 压法 | 请求数 | 实际 RPS | P50 ms | P95 ms | P99 ms | 失败率 |", "|" + "---|" * 8]
    table += ["| " + " | ".join(r) + " |" for r in rows]
    out.write_text("\n".join([
        "# 压测报告",
        "",
        f"> 由 `make perf` 生成 · {datetime.now(UTC):%Y-%m-%d %H:%M UTC} · 工具 k6，固定到达率执行器"
        "（从计划发送时刻计时，不会因服务变慢而少发请求，避免低估 P99）",
        "",
        "## 机器",
        "",
        f"- 宿主：{cpu}，macOS {platform.mac_ver()[0]}",
        f"- 容器：colima 虚拟机（vz），{vm}；ClickHouse 24.8、Redis 7、API（uvicorn 2 个 worker）同机",
        "- k6 与服务同机运行，会互相抢 CPU，数字偏保守",
        "",
        "## P1–P4 结果",
        "",
        *table,
        "",
        f"**P3 击穿**：500 个并发请求打同一个冷 key，存储层实际收到的趋势查询 **{herd_q} 次**。"
        "singleflight 是进程内的，API 起了 2 个 worker，所以上限是 2 次；没有 singleflight 时是 500 次。"
        "多实例部署要做到全局 1 次，需要把锁放到 Redis（`SET NX` + 短 TTL），代价是多一次网络往返。",
        "",
        f"**P4 降级**：停掉 ClickHouse 后，已缓存过的 key 返回 **{deg.get('cached_key_status', '?')}**"
        f"（`{deg.get('cached_key_meta', '?')}`，响应头 `{deg.get('cached_key_stale_header') or '?'}`）；"
        "从没缓存过的 key 返回 "
        f"**{deg.get('uncached_key_status', '?')} {deg.get('uncached_key_code', '')}**（RFC 9457 错误体）。",
        "",
        *p5_section(results),
        *p6_section(results),
        *(manual or [MARKER, "", "（手写分析放在这里，重新生成时保留。）", ""]),
    ]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
