"""P5 千万级实测：在同一个 ClickHouse 里建独立库（默认 p5），合成约 1000 万行明细，测存储量、重算耗时、
各段查询延迟，以及端到端（缓存全部未命中）对照延迟目标。

用法：uv run python perf/p5.py [--rows 10000000] [--db p5] [--keep] [--e2e-rates 10,20,50,100,200]
      结果写 perf/results/p5.json 与 p5_e2e_*.json，由 perf/report.py 汇总进 docs/perf-report.md。

测量分两组，用来坐实「part 数对耗时的影响」：
  rollup     重算完、等后台合并停下后立刻测（v1.2.0 的 P5 就是这个状态：每个月 4 个 part）
  optimized  对全部表 OPTIMIZE … FINAL，每个月压成 1 个 part 后再测
每组都测这些查询：日表段一年、品类 15 分钟表段一年（Berlin / Kathmandu）、商品 15 分钟表段一年（对照：
v1.2.0 的 B 段读的就是商品层）、明细段 31 天（v1.2.0 下 Kathmandu 只能整段扫明细），以及实时请求里的明细段：
优化前是未封口的一整天，优化后只剩封口线之后的半天尾巴。

为什么独立库而不是另起一个 ClickHouse：DDL 里表名不带库名，`--database p5` 就能原样建一套，
不碰样例库 salestrend；API 也只靠 CH_DATABASE 环境变量切库，端到端压测不用改代码。
默认测完 DROP DATABASE；--keep 保留（之后手动 `DROP DATABASE p5` 清理）。

延迟一律从 system.query_log 取（服务端耗时，按 log_comment 区分每组），不含 docker exec 与网络开销。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from salestrend.query.planner import local_day_utc

sys.path.insert(0, str(ROOT / "perf"))
from sampler import run_k6

DC = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "exec", "-T", "clickhouse"]
AUTH = ["-u", os.getenv("CH_USER", "salestrend"), "--password", os.getenv("CH_PASSWORD", "salestrend")]
PRESET = ["UTC", "Asia/Shanghai", "America/Los_Angeles", "America/New_York", "Europe/London", "Asia/Kolkata"]
PLATFORMS = ["app", "marketplace", "website"]
# 规则版本直接用 v1：API 只认 rules/ 下的版本，默认版本就是 v1，端到端不用再复制一份成员表
CLS, RV = "maxi-dress", "v1"

# 5000 个商品 × 3 平台，2024 全年每天 07:00–22:59 均匀分布，销量 1–18。
# sipHash 按 number 派生各列：可复现、无相关性；event_key = hex(sipHash128(number)) 保证唯一
GENERATE = """
INSERT INTO fact_sales
SELECT hex(sipHash128(number)),
       toUInt32(1 + sipHash64(number, 1) % 5000),
       ['app', 'marketplace', 'website'][1 + sipHash64(number, 2) % 3],
       toUInt32(1 + sipHash64(number, 3) % 18),
       addSeconds(toDateTime64('2024-01-01 07:00:00', 3, 'UTC'),
                  (sipHash64(number, 4) % 366) * 86400 + sipHash64(number, 5) % (16 * 3600)),
       now64(3, 'UTC')
FROM numbers({rows})
SETTINGS max_partitions_per_insert_block = 100
"""
# 每 10 个商品 1 个 match：500 个，占 10%。其余 exclude（查询只取 match，和真实成员表的选择性一致）
MEMBERS = """
INSERT INTO product_class_member
SELECT 'maxi-dress', 'v1', toUInt32(number + 1), if((number + 1) % 10 = 0, 'match', 'exclude'),
       if((number + 1) % 10 = 0, 'P5_match', 'P5_other'), [], now64(3, 'UTC')
FROM numbers(5000)
"""


def ch(sql: str, db: str, comment: str = "", fmt: str = "TSV", stdin: str | None = None) -> str:
    # 灌数与重算的大 GROUP BY 显式限内存、超了落盘：ClickHouse 默认按整机内存的 90% 估上限，
    # 和 API、Redis 混部在 6 GiB 虚拟机里时，没到它自己的上限就先被内核 OOM 杀掉（10-23 实测 exit 137）
    args = [*DC, "clickhouse-client", *AUTH, "--database", db, "--format", fmt,
            "--max_memory_usage", "2500000000", "--max_bytes_before_external_group_by", "1000000000"]
    if comment:
        args += ["--log_comment", comment]
    if stdin is not None:
        args += ["--multiquery"]
    else:
        args += ["-q", sql]
    # 没有输入时显式给 /dev/null：后台运行时继承来的 stdin 不会关闭，docker exec 偶尔会一直等它（实测卡死过一次）
    io = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    r = subprocess.run(args, capture_output=True, text=True, check=False, timeout=900, **io)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:2000])
    return r.stdout.strip()


def literal(value, typ: str) -> str:
    if typ.startswith("Array"):
        return "[" + ",".join(f"'{v}'" for v in value) + "]"
    if typ.startswith("DateTime"):
        return f"toDateTime('{value:%Y-%m-%d %H:%M:%S}', 'UTC')"
    if typ in ("String", "Date"):
        return f"'{value}'"
    return str(value)


def render(path: str, params: dict) -> list[str]:
    """把 sql/ 下的文件渲染成带字面量的单行语句：clickhouse-benchmark 一行一条，且不认 {name:Type} 参数。"""
    text = (ROOT / "sql" / path).read_text()
    text = "\n".join(re.sub(r"--.*$", "", ln) for ln in text.splitlines())   # 去行内注释
    stmts = [s.strip() for s in text.split(";") if s.strip()]
    sub = lambda m: literal(params[m.group(1)], m.group(2))
    return [" ".join(re.sub(r"\{(\w+):([^}]+)\}", sub, s).split()) for s in stmts]


def utc_range(d1: date, d2: date, tz: str) -> tuple[datetime, datetime]:
    z = ZoneInfo(tz)
    return local_day_utc(d1, z)[0], local_day_utc(d2, z)[1]


def wait_merges(db: str) -> None:
    for _ in range(120):
        if ch(f"SELECT count() FROM system.merges WHERE database = '{db}'", db) == "0":
            return
        time.sleep(2)


def log_stats(db: str, comment: str) -> dict:
    ch("SYSTEM FLUSH LOGS", db)
    row = ch(f"""
        SELECT count(), quantiles(0.5, 0.95, 0.99)(query_duration_ms), max(query_duration_ms),
               round(avg(read_rows)), round(avg(read_bytes)), round(avg(memory_usage))
        FROM system.query_log WHERE log_comment = '{comment}' AND type = 'QueryFinish'""", db, fmt="JSONCompact")
    n, (p50, p95, p99), mx, rr, rb, mem = json.loads(row)["data"][0]
    # 失败的查询单独计数：并发高了会撞内存上限（MEMORY_LIMIT_EXCEEDED），这本身就是拐点的一部分
    errors = ch(f"SELECT count(), any(exception_code) FROM system.query_log WHERE log_comment = '{comment}' "
                "AND type IN ('ExceptionBeforeStart', 'ExceptionWhileProcessing')", db).split("\t")
    return {"n": int(n), "p50": p50, "p95": p95, "p99": p99, "max": mx, "read_rows": int(rr or 0),
            "read_bytes": int(rb or 0), "memory": int(mem or 0), "errors": int(errors[0]),
            "error_code": int(errors[1]) if int(errors[0]) else None}


def timed(db: str, comment: str, stmts: list[str]) -> dict:
    t0 = time.perf_counter()
    ch("", db, comment=comment, stdin=";\n".join(stmts) + ";")
    wall = time.perf_counter() - t0
    ch("SYSTEM FLUSH LOGS", db)
    ms, written = ch(f"SELECT sum(query_duration_ms), sum(written_rows) FROM system.query_log "
                     f"WHERE log_comment = '{comment}' AND type = 'QueryFinish'", db).split("\t")
    return {"wall_s": round(wall, 2), "server_ms": int(ms), "written_rows": int(written)}


def bench(db: str, comment: str, sql: str, conc: int, iters: int) -> dict:
    args = [*DC, "clickhouse-benchmark", *AUTH, "--database", db, "-c", str(conc), "-i", str(iters),
            f"--log_comment={comment}", "--continue_on_errors"]
    t0 = time.perf_counter()
    r = subprocess.run(args, input=sql + "\n", capture_output=True, text=True, check=False)
    if r.returncode and "Exception" not in r.stderr:  # 查询级错误由 log_stats 计数；这里只拦工具本身起不来
        raise RuntimeError(f"clickhouse-benchmark 失败：{r.stderr[-500:]}")
    # QPS 用墙钟算（含约 0.3s 的 docker exec 启动，略偏低）：benchmark 的 QPS 行是每秒一报的区间值，不是全程
    st = log_stats(db, comment)
    return {**st, "conc": conc, "qps": round(st["n"] / (time.perf_counter() - t0), 1)}


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check).stdout.strip()


TABLES = ["fact_sales", "agg_product_15m", "agg_class_15m", "agg_class_daily", "product_class_member"]
# 延迟目标（端到端，缓存未命中）：已聚合的段 50–100 ms，含当天实时数据 ≤ 150 ms
E2E = {
    "agg_hourly": ("已聚合 · 一年 · Europe/Berlin（品类 15 分钟表段）", 100),
    "agg_kathmandu": ("已聚合 · 一年 · Asia/Kathmandu（品类 15 分钟表段）", 100),
    "agg_daily": ("已聚合 · 一年 · UTC（日表段）", 100),
    "rt_utc": ("实时 · 近 30 天 · UTC（日表段 + 当天明细段）", 150),
    "rt_berlin": ("实时 · 近 30 天 · Europe/Berlin（品类 15 分钟表段 + 当天明细段）", 150),
    "rt_kathmandu": ("实时 · 近 30 天 · Asia/Kathmandu（非整点非预设）", 150),
}


def storage(db: str) -> list[dict]:
    return json.loads(ch(f"""
        SELECT table, sum(rows) AS rows, sum(data_uncompressed_bytes) AS raw_bytes,
               sum(data_compressed_bytes) AS bytes, round(raw_bytes / bytes, 2) AS ratio, count() AS parts,
               uniqExact(partition) AS partitions
        FROM system.parts WHERE database = '{db}' AND active GROUP BY table ORDER BY bytes DESC""",
        db, fmt="JSON"))["data"]


def set_state(db: str, sealed: str, through: dict[str, str]) -> None:
    rows = [("sealed_until", sealed), ("data_through", max(through.values()))]
    rows += [(f"data_through:{s}", t) for s, t in through.items()]
    ch("INSERT INTO pipeline_state VALUES " + ",".join(f"('{k}', '{v}', now64(3))" for k, v in rows), db)


def measure(db: str, tag: str, queries: dict[str, str], runs: int) -> dict:
    """每条路径：先比对带 / 不带 FINAL 的结果，再测串行、8 并发、16 并发。"""
    out = {}
    for name, sql in queries.items():
        variants = {"final": sql, "nofinal": sql.replace(" FINAL", "")}
        # 结果一样才谈得上「FINAL 是不是多余」
        sums = {k: ch(f"SELECT sum(sales), count() FROM ({v})", db) for k, v in variants.items()}
        q = {"sql": sql, "same_result": sums["final"] == sums["nofinal"], "result": sums["final"]}
        for k, v in variants.items():
            ch("", db, comment=f"{tag}-warm", stdin=";\n".join([v] * 3) + ";")  # 预热页缓存
            t = f"{tag}-{name}-{k}"
            ch("", db, comment=f"{t}-serial", stdin=";\n".join([v] * runs) + ";")
            q[f"{k}_serial"] = log_stats(db, f"{t}-serial")
            q[f"{k}_c8"] = bench(db, f"{t}-c8", v, 8, max(runs * 2, 120))
            q[f"{k}_c16"] = bench(db, f"{t}-c16", v, 16, max(runs * 2, 160))
            print(tag, name, k, q[f"{k}_serial"]["p99"], q[f"{k}_c8"]["p99"], q[f"{k}_c16"]["p99"], flush=True)
        out[name] = q
    return out


def k6(out: Path, scen: str, rate: int, duration: str) -> dict:
    f = out / f"p5_e2e_{scen}_r{rate}.json"
    return run_k6(["-e", "SCEN=p5", "-e", f"P5={scen}", "-e", "API=http://localhost:8001", "-e", f"RATE={rate}",
                   "-e", f"DURATION={duration}", str(ROOT / "perf/k6/scenarios.js")], f, float(duration.rstrip("s")))


def e2e(db: str, out: Path, rates: list[int], duration: str, scens: list[str]) -> None:
    """另起一个指向 p5 库的 API 实例（同一镜像，只改 CH_DATABASE），k6 固定到达率压四个场景。

    缓存旁路：REDIS_URL 指向一个拒绝连接的端口。ResponseCache 把 Redis 故障一律当未命中（设计如此），
    连接被拒是立即返回的，不会吃 0.3s 超时；每个请求都真打存储。端到端数字因此是「全部未命中」的最坏情况。
    到达率从低往高爬；某一档 P99 超过目标的 2 倍或失败率超过 1%，更高的档不再跑（只会更差）。
    每档同时采样容器与 k6 的 CPU（perf/sampler.py），用来判断拐点处的瓶颈在哪一侧。
    """
    out.mkdir(parents=True, exist_ok=True)
    for f in [f for s in scens for f in out.glob(f"p5_e2e_{s}_r*.json")]:
        f.unlink()  # 上一轮的档位可能比这轮多，留着会混进报告
    api = docker("compose", "-f", str(ROOT / "docker-compose.yml"), "ps", "-q", "api")
    image = docker("inspect", "-f", "{{.Config.Image}}", api)
    net = docker("inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}", api)
    name = "sales-trend-api-p5"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--name", name, "--network", net, "-p", "8001:8000", "-e", "CH_HOST=clickhouse",
           "-e", f"API_WORKERS={os.getenv('API_WORKERS', '2')}",
           "-e", f"CH_DATABASE={db}", "-e", f"CH_USER={AUTH[1]}", "-e", f"CH_PASSWORD={AUTH[3]}",
           "-e", "REDIS_URL=redis://127.0.0.1:1/0", "-e", "RATE_LIMIT_PER_S=100000", "-e", "RATE_LIMIT_BURST=100000",
           image)
    realtime = False
    try:
        for _ in range(60):
            if subprocess.run(["curl", "-sf", "http://localhost:8001/healthz"], capture_output=True,
                              check=False).returncode == 0:
                break
            time.sleep(1)
        for scen in scens:
            if scen.startswith("rt_") and not realtime:
                # 实时场景：最后一天中午封口、数据截止 12:30，这一天未封口、走明细段；其余天走汇总。
                # 明细里 12:30 之后的事件仍在（合成数据覆盖全天），所以明细段读的是一整天，偏保守
                set_state(db, "2024-12-31 12:00:00", {s: "2024-12-31 12:30:00" for s in PLATFORMS})
                time.sleep(7)  # API 进程内缓存水位 5 秒
                realtime = True
            for rate in rates:
                r = k6(out, scen, rate, duration)
                print("e2e", scen, rate, {k: r[k] for k in ("p99", "failed")}, r["cpu"], flush=True)
                # 找拐点：超过目标的 2 倍或失败率过 1% 就停，更高的档只会更差
                if r["p99"] > 2 * E2E[scen][1] or r["failed"] > 0.01:
                    break
    finally:
        docker("rm", "-f", name, check=False)
        set_state(db, "2025-01-01 00:00:00", {s: "2024-12-31 22:59:59" for s in PLATFORMS})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--db", default="p5")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-load", action="store_true", help="库已建好（且已合并），只重跑 optimized 组与端到端")
    ap.add_argument("--e2e-rates", default="10,20,50,100,200,300,400,600,800,1000,1200",
                    help="端到端 k6 到达率（RPS），逗号分隔；空串跳过")
    ap.add_argument("--e2e-duration", default="60s")
    ap.add_argument("--e2e-only", action="store_true", help="库已建好：只跑端到端")
    ap.add_argument("--e2e-scens", default=",".join(E2E), help="端到端场景，逗号分隔")
    ap.add_argument("--e2e-out", default="perf/results", help="端到端结果目录（优化前后对照时分开放）")
    a = ap.parse_args()
    db, run_id = a.db, datetime.now(UTC).strftime("%H%M%S")
    out = ROOT / "perf" / "results"
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {"rows_requested": a.rows, "db": db, "phases": {}}
    if a.skip_load and (out / "p5.json").exists():  # 只重跑后半段时保留上次的写入、重算与 rollup 组
        prev = json.loads((out / "p5.json").read_text())
        res.update({k: prev[k] for k in ("load", "rollup_product", "rollup_class_15m", "rollup_daily", "optimize")
                    if k in prev})
        res["phases"].update({k: v for k, v in prev.get("phases", {}).items() if k == "rollup"})

    hs, he = utc_range(date(2024, 1, 1), date(2024, 12, 31), "Europe/Berlin")
    ks, ke = utc_range(date(2024, 1, 1), date(2024, 12, 31), "Asia/Kathmandu")
    fs, fe = utc_range(date(2024, 6, 1), date(2024, 7, 1), "Asia/Kathmandu")
    ds, de = utc_range(date(2024, 12, 31), date(2024, 12, 31), "UTC")
    noon = datetime(2024, 12, 31, 12, tzinfo=UTC)  # 实时场景的封口线
    base = {"cls": CLS, "rv": RV, "platforms": PLATFORMS}
    queries = {
        "class_daily": render("queries/trend_daily.sql", {**base, "tz": "UTC", "d1": date(2024, 1, 1),
                                                           "d2": date(2024, 12, 31)})[0],
        "class_15m": render("queries/trend_class_15m.sql", {**base, "tz": "Europe/Berlin", "s": hs, "e": he})[0],
        "class_15m_ktm": render("queries/trend_class_15m.sql", {**base, "tz": "Asia/Kathmandu", "s": ks, "e": ke})[0],
        "product_15m": render("queries/trend_product_15m.sql", {**base, "tz": "Europe/Berlin", "s": hs, "e": he})[0],
        "fact": render("queries/trend_fact.sql", {**base, "tz": "Asia/Kathmandu", "s": fs, "e": fe})[0],
        "fact_day": render("queries/trend_fact.sql", {**base, "tz": "UTC", "s": ds, "e": de})[0],
        "fact_tail": render("queries/trend_fact.sql", {**base, "tz": "UTC", "s": noon, "e": de})[0],
    }

    if not a.skip_load:
        ch(f"DROP DATABASE IF EXISTS {db}", "default")
        ch(f"CREATE DATABASE {db}", "default")
        ch("", db, stdin=(ROOT / "sql" / "ddl" / "001_tables.sql").read_text())
        print("ddl ok", flush=True)
        res["load"] = timed(db, f"p5-{run_id}-load", [GENERATE.format(rows=a.rows).strip(), MEMBERS.strip()])
        print("load", res["load"], flush=True)
        wait_merges(db)

        # 重算顺序与 store.rollup 相同：商品 15 分钟表 → 品类 15 分钟表（从商品层汇总）→ 品类日表（从明细）
        ver = int(time.time() * 1000)
        year = {"from": datetime(2024, 1, 1, tzinfo=UTC), "to": datetime(2025, 1, 1, tzinfo=UTC), "version": ver}
        # 商品 15 分钟层按月分批回填：全年一条 GROUP BY 要约 5.2 GiB，在 6 GiB 的 colima 虚拟机上稳定撞内存上限
        # （code 241，10-23 连续两次）。生产大批量回填同理按分区分批，日常重算本来只算最近几个小时
        months = [datetime(2024, m, 1, tzinfo=UTC) for m in range(1, 13)] + [year["to"]]
        res["rollup_product"] = timed(db, f"p5-{run_id}-rollup-product", [
            s for f, t in itertools.pairwise(months)
            for s in render("rollup/product_15m.sql", {**year, "from": f, "to": t})])
        print("rollup product 15m", res["rollup_product"], flush=True)
        res["rollup_class_15m"] = timed(db, f"p5-{run_id}-rollup-class-15m", render(
            "rollup/class_15m.sql", {**year, "cls": CLS, "rv": RV}))
        print("rollup class 15m", res["rollup_class_15m"], flush=True)
        res["rollup_daily"] = {}
        for tz in PRESET:
            f, t = utc_range(date(2024, 1, 1), date(2024, 12, 31), tz)
            res["rollup_daily"][tz] = timed(db, f"p5-{run_id}-rollup-daily-{tz}", render("rollup/class_daily.sql", {
                "cls": CLS, "rv": RV, "tz": tz, "d1": date(2024, 1, 1), "d2": date(2024, 12, 31),
                "from": f, "to": t, "version": ver}))
            print("rollup daily", tz, res["rollup_daily"][tz], flush=True)
        # 水位：模拟「2024 年已全部封口」
        set_state(db, "2025-01-01 00:00:00", {s: "2024-12-31 22:59:59" for s in PLATFORMS})
        wait_merges(db)

        # 第一组：重算后立刻测（后台合并已停下，但每个月仍有多个 part）
        res["phases"]["rollup"] = {"storage": storage(db)}
        print("storage rollup", res["phases"]["rollup"]["storage"], flush=True)
        res["phases"]["rollup"]["queries"] = measure(db, f"p5-{run_id}-rollup", queries, a.runs)

        # 第二组：OPTIMIZE FINAL 把每个分区压成 1 个 part 再测，坐实 part 数对 FINAL 的影响
        res["optimize"] = {}
        for t in TABLES:
            res["optimize"][t] = timed(db, f"p5-{run_id}-optimize-{t}", [f"OPTIMIZE TABLE {t} FINAL"])
        wait_merges(db)

    if a.e2e_only:
        e2e(db, ROOT / a.e2e_out, [int(x) for x in a.e2e_rates.split(",")], a.e2e_duration, a.e2e_scens.split(","))
        return
    res["phases"]["optimized"] = {"storage": storage(db)}
    print("storage optimized", res["phases"]["optimized"]["storage"], flush=True)
    res["phases"]["optimized"]["queries"] = measure(db, f"p5-{run_id}-optimized", queries, a.runs)
    (out / "p5.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out / 'p5.json'}")
    if a.e2e_rates:
        e2e(db, out, [int(x) for x in a.e2e_rates.split(",")], a.e2e_duration, a.e2e_scens.split(","))
    if not a.keep:
        ch(f"DROP DATABASE {db}", "default")
        print(f"dropped {db}")


if __name__ == "__main__":
    main()
