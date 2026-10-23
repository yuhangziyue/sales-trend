"""把 k6 --summary-export、/metrics 快照与 perf/p5.py 的结果汇总成 docs/perf-report.md。

报告里「## 结论与解读」之后是手写分析：重新生成时原样保留，只刷新上面由数据生成的表格。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from itertools import pairwise
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
# 存储层分段目标（单段 SQL 服务端 P99），商品小时表段沿用 v1.2.0 的 B 段目标作对照
SLO_P5 = {"class_daily": ("日表段 · 一年 · UTC（预设）", 150),
          "class_15m": ("品类 15 分钟表段 · 一年 · Europe/Berlin", 500),
          "class_15m_ktm": ("品类 15 分钟表段 · 一年 · Asia/Kathmandu", 500),
          "product_15m": ("商品 15 分钟表段（对照，不在查询路径） · 一年 · Europe/Berlin", 500),
          "fact": ("明细段 · 31 天 · Asia/Kathmandu（优化前该时区只能整段扫明细）", 1500),
          "fact_day": ("明细段 · 实时优化前 · 未封口的一整天（UTC 12-31）", 150),
          "fact_tail": ("明细段 · 实时优化后 · 封口线之后的尾巴（12:00–24:00）", 150)}
PHASES = {"rollup": "重算后", "optimized": "OPTIMIZE 后"}
# 端到端目标（缓存未命中）：已聚合 50–100 ms，含当天实时 ≤ 150 ms。场景名与 perf/p5.py 的 E2E 一致
E2E = {"agg_hourly": ("已聚合 · 一年 · Europe/Berlin → 品类 15 分钟表段", 100),
       "agg_kathmandu": ("已聚合 · 一年 · Asia/Kathmandu → 品类 15 分钟表段", 100),
       "agg_daily": ("已聚合 · 一年 · UTC → 日表段", 100),
       "rt_utc": ("实时 · 近 30 天 · UTC → 日表 + 当天前半天 15 分钟表 + 尾巴明细", 150),
       "rt_berlin": ("实时 · 近 30 天 · Europe/Berlin → 15 分钟表 + 尾巴明细", 150),
       "rt_kathmandu": ("实时 · 近 30 天 · Asia/Kathmandu → 15 分钟表 + 尾巴明细", 150)}


def mb(n) -> str:
    n = int(n)
    return f"{n / 1e6:,.1f} MB" if n >= 1e6 else f"{n / 1e3:,.1f} KB"


def fmt_ms(x) -> str:
    return f"{float(x or 0):.0f}" if float(x or 0) >= 10 else f"{float(x or 0):.1f}"


def load_runs(results: Path, prefix: str) -> dict[str, list[tuple[int, dict]]]:
    """{场景: [(到达率, k6 metrics + 可选的 _cpu)]}。文件名 {prefix}{场景}_r{到达率}.json，旁边可有 .cpu.json。"""
    out: dict[str, list[tuple[int, dict]]] = {}
    for f in results.glob(f"{prefix}*_r*.json"):
        if f.name.endswith(".cpu.json"):
            continue
        scen, rate = f.stem.removeprefix(prefix).rsplit("_r", 1)
        m = json.loads(f.read_text())["metrics"]
        cpu = f.with_suffix(".cpu.json")
        if cpu.exists():
            m["_cpu"] = json.loads(cpu.read_text())
        out.setdefault(scen, []).append((int(rate), m))
    return {k: sorted(v, key=lambda x: x[0]) for k, v in out.items()}


def e2e_rows(results: Path) -> dict[str, list[tuple[int, dict]]]:
    return load_runs(results, "p5_e2e_")


def cpu_cols(m: dict) -> str:
    """API / ClickHouse / k6 的 CPU%（1 核 = 100%，虚拟机 4 vCPU 上限约 400%）。"""
    c = m.get("_cpu")
    if not c:
        return "— | — | —"
    api = sum(v for k, v in c.items() if "api" in k and v)
    ch = sum(v for k, v in c.items() if "clickhouse" in k and v)
    return f"{api:.0f}% | {ch:.0f}% | {c.get('k6(host)') or 0:.0f}%"


def capacity_section(results: Path) -> list[str]:
    runs = load_runs(results, "capacity_hit_")
    if not runs:
        return []
    lines = ["## 容量：缓存命中路径的单机拐点", "",
             "`make capacity`：样例库，20 个热点 key 预热后全部命中，k6 固定到达率每档 30 s，逐档加压直到 P99 超过 "
             "40 ms（目标 20 ms 的 2 倍）或失败率超过 1%。每个 worker 数各重建一次 api 容器。CPU 列为采样平均，"
             "1 核 = 100%：API / ClickHouse 在 4 vCPU 虚拟机里（上限约 400%），k6 在宿主机上。", "",
             "| API worker | 到达率 | 实际 RPS | P50 ms | P99 ms | 失败率 | 目标 P99 ≤ 20 | API CPU | ClickHouse CPU | k6 CPU |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for w, pts in sorted(runs.items()):
        for rate, m in pts:
            d, f = m["http_req_duration"], m["http_req_failed"]["value"]
            ok = d["p(99)"] <= 20 and f < 0.001
            lines.append(f"| {w.removeprefix('w')} | {rate} | {m['http_reqs']['rate']:.0f} | {d['med']:.1f} | "
                         f"{d['p(99)']:.1f} | {f * 100:.2f}% | {'✅' if ok else '❌'} | {cpu_cols(m)} |")
    return [*lines, ""]


def p5_section(results: Path) -> list[str]:
    f = results / "p5.json"
    if not f.exists():
        return ["## P5 千万级实测", "", "未运行（`make perf-p5`）。", ""]
    r = json.loads(f.read_text())
    phases = {k: v for k, v in r["phases"].items() if k in PHASES}
    lines = ["## P5 千万级实测", "",
             f"`make perf-p5`：同一个 ClickHouse 里建独立库 `{r['db']}`（同一份 DDL），"
             f"`INSERT … SELECT … FROM numbers({r['rows_requested']:,})` 合成明细：5000 个商品 × 3 个平台，"
             "2024 全年每天 07:00–22:59 均匀分布，销量 1–18；成员表规则版本 `v1` 下 match 500 个商品（每 10 个取 1 个）。"
             "SQL 层延迟一律取 `system.query_log` 的服务端耗时。测量分两组：**重算后**（等后台合并停下就测，"
             "与 v1.2.0 的 P5 同一状态）与 **OPTIMIZE 后**（全部表 `OPTIMIZE … FINAL`，每个月分区压成 1 个 part）。", "",
             "### 存储量与 part 数（system.parts，active parts）", "",
             "| 表 | 行数 | 压缩前 | 压缩后 | 压缩比 | 分区 | " + " | ".join(f"parts（{PHASES[k]}）" for k in phases) + " |",
             "|" + "---|" * (6 + len(phases))]
    first = next(iter(phases.values()))["storage"]
    parts = {k: {t["table"]: t["parts"] for t in v["storage"]} for k, v in phases.items()}
    for t in first:
        lines.append(f"| {t['table']} | {int(t['rows']):,} | {mb(t['raw_bytes'])} | {mb(t['bytes'])} | {t['ratio']} | "
                     f"{t.get('partitions', '?')} | " + " | ".join(str(parts[k].get(t["table"], "?")) for k in phases) + " |")
    if "load" in r:
        lines += ["", "### 写入与重算耗时", "", "| 步骤 | 服务端耗时 | 写入行数 |", "|---|---|---|",
                  f"| 合成明细 + 成员表 | {r['load']['server_ms'] / 1000:.1f} s | {r['load']['written_rows']:,} |",
                  f"| 商品 15 分钟表重算（全年，从明细，先删后写） | {r['rollup_product']['server_ms'] / 1000:.1f} s | "
                  f"{r['rollup_product']['written_rows']:,} |"]
        if "rollup_class_15m" in r:
            v = r["rollup_class_15m"]
            lines.append(f"| 品类 15 分钟表重算（全年，从商品层，一个规则版本） | {v['server_ms'] / 1000:.2f} s | "
                         f"{v['written_rows']:,} |")
        for tz, v in r["rollup_daily"].items():
            lines.append(f"| 品类日表重算 · {tz}（全年，从明细） | {v['server_ms'] / 1000:.2f} s | {v['written_rows']:,} |")
        for t, v in r.get("optimize", {}).items():
            lines.append(f"| OPTIMIZE TABLE {t} FINAL | {v['server_ms'] / 1000:.2f} s | — |")
    lines += ["", "### 各段查询 × 两组（串行 = 同一条 SQL 连跑 N 次；并发 = clickhouse-benchmark -c 8 / -c 16）", "",
              "| 段 | 组 | FINAL | 串行 P50 / P99 ms | 8 并发 P50 / P99 ms | 8 并发 QPS | 16 并发 P50 / P99 ms | "
              "16 并发 QPS | 16 并发失败 | read_rows | 峰值内存 | 目标 P99（串行 / 8 / 16） |",
              "|" + "---|" * 12]
    for name, (label, target) in SLO_P5.items():
        for ph, pv in phases.items():
            q = pv["queries"].get(name)
            if not q:
                continue
            for k, lab in (("final", "有"), ("nofinal", "去掉")):
                se, c8, c16 = q[f"{k}_serial"], q[f"{k}_c8"], q.get(f"{k}_c16") or {}
                ok = " / ".join("✅" if (x.get("p99") or 1e9) <= target else "❌" for x in (se, c8, c16))
                err = f"{c16['errors']}（code {c16['error_code']}）" if c16.get("errors") else "0"
                lines.append(
                    f"| {label} | {PHASES[ph]} | {lab} | {fmt_ms(se['p50'])} / {fmt_ms(se['p99'])} | "
                    f"{fmt_ms(c8['p50'])} / {fmt_ms(c8['p99'])} | {c8['qps'] or 0:.1f} | "
                    f"{fmt_ms(c16.get('p50'))} / {fmt_ms(c16.get('p99'))} | {c16.get('qps') or 0:.1f} | {err} | "
                    f"{se['read_rows']:,} | {mb(se['memory'])} | ≤ {target}（{ok}） |")
    lines += ["", "结果核对（`sum(sales), 行数`，各组带 / 不带 FINAL 是否一致）："]
    for ph, pv in phases.items():
        lines.append(f"- {PHASES[ph]}：" + "；".join(
            f"{n} `{q['result']}` {'一致' if q['same_result'] else '**不一致**'}" for n, q in pv["queries"].items()))
    e2e = e2e_rows(results)
    if e2e:
        lines += ["", "### 端到端：对照延迟目标（指向 p5 库的 API 实例，k6 固定到达率每档 60s，缓存全部未命中）", "",
                  "目标：已聚合的数据 P99 ≤ 100 ms（50–100 ms 以内），含当天实时数据 P99 ≤ 150 ms。"
                  "API 2 个 uvicorn worker；ClickHouse 与 API 跑在同一台 4 vCPU 虚拟机里，k6 在同一台宿主机上发压。"
                  "某档 P99 超过目标的 2 倍或失败率超过 1% 后，更高的档不再跑。CPU 列为该档采样平均（1 核 = 100%，"
                  "API / ClickHouse 在 4 vCPU 虚拟机里，上限约 400%；k6 在宿主机上）。", "",
                  "| 场景 | 到达率 | 请求数 | P50 ms | P95 ms | P99 ms | 失败率 | 目标 P99 | 达标 | API CPU | ClickHouse CPU | k6 CPU |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        best = {}
        for scen, (label, target) in E2E.items():
            for rate, m in e2e.get(scen, []):
                d, reqs, failed = m["http_req_duration"], m["http_reqs"], m["http_req_failed"]
                ok = d["p(99)"] <= target and failed["value"] < 0.001
                if ok:
                    best[scen] = max(best.get(scen, 0), rate)
                lines.append(f"| {label} | {rate} RPS | {reqs['count']} | {d['med']:.0f} | {d['p(95)']:.0f} | "
                             f"{d['p(99)']:.0f} | {failed['value'] * 100:.2f}% | ≤ {target} | {'✅' if ok else '❌'} | "
                             f"{cpu_cols(m)} |")
        before = e2e_rows(results / "before")
        if before:
            lines += ["", "#### 实时路径优化前后（同样的到达率；优化前 = 未封口的一整天读明细，"
                      "Kathmandu 整段读明细；优化后 = 只有封口线之后的尾巴读明细、明细按 event_ts 打头）", "",
                      "| 场景 | 到达率 | 优化前 P50 / P95 / P99 ms | 优化前失败率 | 优化后 P50 / P95 / P99 ms | 优化后失败率 |",
                      "|---|---|---|---|---|---|"]
            for scen in ("rt_utc", "rt_berlin", "rt_kathmandu"):
                after = dict(e2e.get(scen, []))
                for rate, m in before.get(scen, []):
                    b = m["http_req_duration"]
                    a = after.get(rate)
                    ad = a["http_req_duration"] if a else None
                    lines.append(
                        f"| {E2E[scen][0].split(' → ')[0]} | {rate} RPS | {b['med']:.0f} / {b['p(95)']:.0f} / "
                        f"{b['p(99)']:.0f} | {m['http_req_failed']['value'] * 100:.2f}% | "
                        + (f"{ad['med']:.0f} / {ad['p(95)']:.0f} / {ad['p(99)']:.0f} | "
                           f"{a['http_req_failed']['value'] * 100:.2f}% |" if a else "未测 | — |"))
        lines += ["", "仍满足目标的最高到达率（所测档位内）：" + "；".join(
            f"{E2E[s][0].split(' → ')[0]} **{best[s]} RPS**" if s in best else f"{E2E[s][0].split(' → ')[0]} **无**"
            for s in E2E if s in e2e) + "。"]
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


CACHE_KINDS = ("hit", "block", "block_partial", "miss", "stale")


def _delta(before: dict, after: dict) -> dict:
    return {key: v - before.get(key, 0.0) for key, v in after.items()}


def _total(delta: dict, name: str, **want) -> float:
    return sum(v for (n, lbl), v in delta.items() if n == name and all(dict(lbl).get(x) == y for x, y in want.items()))


def _rates(delta: dict) -> dict:
    """三档命中率与回源率，公式见 design.md §7.2。分母 = 走到缓存的请求（不含 none）。"""
    cache = {c: _total(delta, "trend_requests_total", cache=c) for c in (*CACHE_KINDS, "none")}
    served = sum(cache[c] for c in CACHE_KINDS)
    storage = _total(delta, "storage_queries_total")
    r = {"cache": cache, "served": served, "storage": storage}
    for c in CACHE_KINDS:
        r[f"{c}_rate"] = cache[c] / served if served else 0
    r["any_rate"] = r["hit_rate"] + r["block_rate"]                  # 不查库就答上的
    r["backsource"] = storage / served if served else 0
    return r


def p6_stats(results: Path, scen: str) -> dict | None:
    b, a, k = (results / f"{scen}_metrics_before.txt", results / f"{scen}_metrics_after.txt", results / f"{scen}.json")
    if not (b.exists() and a.exists() and k.exists()):
        return None
    before, after = snapshot(b), snapshot(a)
    delta = _delta(before, after)
    paths = {}
    for (n, lbl), v in delta.items():
        if n == "trend_request_seconds_count" and v > 0:
            path = dict(lbl)["path"]
            bk = sorted((float(dict(l2)["le"]), c) for (n2, l2), c in delta.items()
                        if n2 == "trend_request_seconds_bucket" and dict(l2)["path"] == path)
            paths[path] = {"n": int(v), "avg_ms": _total(delta, "trend_request_seconds_sum", path=path) / v * 1000,
                           "p50_ms": (hist_quantile(bk, 0.5) or 0) * 1000, "p99_ms": (hist_quantile(bk, 0.99) or 0) * 1000}
    # 冷启动曲线：run.sh 每 10 秒抓一次 /metrics，相邻快照相减就是这个窗口
    snaps = [before, *[snapshot(f) for f in sorted(results.glob(f"{scen}_metrics_t*.txt"))]]
    windows = [_rates(_delta(x, y)) for x, y in pairwise(snaps)]
    m = json.loads(k.read_text())["metrics"]
    xc = {c: m.get(f"xcache_{c}", {}).get("count", 0) for c in CACHE_KINDS}
    return {**_rates(delta), "storage_by_seg": {s: _total(delta, "storage_queries_total", segment=s)
                                                for s in ("class_daily", "class_15m", "fact")},
            "shared": _total(delta, "singleflight_shared_total"), "paths": paths, "windows": windows,
            "k6_reqs": m["http_reqs"]["count"], "k6_xc": xc,
            "k6_dur": m["http_req_duration"], "k6_failed": m["http_req_failed"]["value"]}


def p6_section(results: Path) -> list[str]:
    names = {"range": "P6 任意区间 · 两层（整响应 + 月块）",
             "range_noblock": "P6 任意区间 · 只有整响应缓存（BLOCK_CACHE=0，同机同轮对照）",
             "hot200": "对照 · 20 个热点 key（P1 的 key 集） · 两层"}
    stats = {s: p6_stats(results, s) for s in names}
    if not any(stats.values()):
        return ["## P6 任意区间命中率", "", "未运行（`make perf-p6`）。", ""]
    lines = ["## P6 任意区间命中率", "",
             "每组都是固定到达率 200 RPS × 120s、开始前清空两层缓存（`trend:*` 与 `blk:*`）。P6 每个请求随机取："
             "起止日期都落在样例数据范围（2024-01-01..03-13，73 天）内、长度 1–73 天均匀（题设 1–90 天，但数据只有 73 天）；"
             "平台 7 种非空组合；tz 6 个预设时区；粒度 day 80% · week 10% · month 10%。对照组只打 P1 的 20 个热点 key。"
             "命中率与回源率取压测前后两次 `/metrics` 快照的差值（两个 worker 已由多进程模式汇总），"
             "再用 k6 按 `X-Cache` 响应头计数交叉验证。三档命中率的公式见 design.md §7.2："
             "第一层 = hit；月块全命中 = block（不查库）；部分命中 = block_partial（用上了月块，但缺块或未封口的日查了库）；"
             "分母都是 hit + block + block_partial + miss + stale。", "",
             "| 场景 | 请求数（/metrics · k6） | hit / block / block_partial / miss / stale | 第一层 | 月块全命中 | 部分命中 | "
             "不查库合计 | k6 X-Cache 不查库 | 存储查询数 | 回源率 | singleflight 合并 | 端到端 P50 / P95 / P99 ms | 失败率 |",
             "|" + "---|" * 13]
    for s, st in stats.items():
        if not st:
            continue
        xc = st["k6_xc"]
        k6_any = (xc["hit"] + xc["block"]) / max(1, sum(xc.values()))
        d, c = st["k6_dur"], st["cache"]
        lines.append(
            f"| {names[s]} | {st['served']:.0f} · {st['k6_reqs']} | "
            f"{' / '.join(f'{c[x]:.0f}' for x in CACHE_KINDS)} | {st['hit_rate'] * 100:.1f}% | "
            f"{st['block_rate'] * 100:.1f}% | {st['block_partial_rate'] * 100:.1f}% | **{st['any_rate'] * 100:.1f}%** | "
            f"{k6_any * 100:.1f}% | {st['storage']:.0f} | **{st['backsource']:.3f}** | {st['shared']:.0f} | "
            f"{d['med']:.1f} / {d['p(95)']:.1f} / {d['p(99)']:.1f} | {st['k6_failed'] * 100:.2f}% |")
    lines += ["", "按 path 的服务端延迟（`trend_request_seconds` 直方图差值；P50 / P99 为桶内插值，精度受桶宽限制）：", "",
              "| 场景 | path | 请求数 | 平均 ms | P50 ms | P99 ms |", "|---|---|---|---|---|---|"]
    for s, st in stats.items():
        for path, v in sorted((st or {}).get("paths", {}).items(), key=lambda x: -x[1]["n"]):
            lines.append(f"| {names[s]} | {path} | {v['n']} | {v['avg_ms']:.1f} | {v['p50_ms']:.1f} | {v['p99_ms']:.1f} |")
    for s in ("range", "range_noblock"):
        st = stats.get(s)
        if st:
            lines += ["", f"{names[s]} 存储查询按段：" +
                      " · ".join(f"{k} {v:.0f}" for k, v in st["storage_by_seg"].items()) + "。"]
    st = stats.get("range")
    if st and st["windows"]:
        lines += ["", "### 冷启动：命中率随时间爬升（P6 任意区间 · 两层）", "",
                  "口径：压测期间每 10 秒抓一次 `/metrics`，相邻两次快照的差值即一个窗口（窗口边界与 k6 起点有约 1 秒的错位，"
                  "首个窗口含 k6 启动）；不预热，从两层全空开始。", "",
                  "| 窗口 | 请求数 | 第一层 | 月块全命中 | 部分命中 | 不查库合计 | 回源率 |", "|---|---|---|---|---|---|---|"]
        for i, w in enumerate(st["windows"]):
            lines.append(f"| {i * 10}–{i * 10 + 10} s | {w['served']:.0f} | {w['hit_rate'] * 100:.1f}% | "
                         f"{w['block_rate'] * 100:.1f}% | {w['block_partial_rate'] * 100:.1f}% | "
                         f"{w['any_rate'] * 100:.1f}% | {w['backsource']:.3f} |")
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
        *capacity_section(results),
        *(manual or [MARKER, "", "（手写分析放在这里，重新生成时保留。）", ""]),
    ]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
