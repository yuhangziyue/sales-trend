"""P5 千万级实测：在同一个 ClickHouse 里建独立库（默认 p5），合成约 1000 万行明细，测存储量、重算耗时、三段查询延迟。

用法：uv run python perf/p5.py [--rows 10000000] [--db p5] [--keep]
      结果写 perf/results/p5.json，由 perf/report.py 汇总进 docs/perf-report.md。

为什么独立库而不是另起一个 ClickHouse：DDL 里表名不带库名，`--database p5` 就能原样建一套，
不碰样例库 salestrend；API 也只靠 CH_DATABASE 环境变量切库，端到端压测不用改代码。
默认测完 DROP DATABASE；--keep 保留（之后手动 `DROP DATABASE p5` 清理）。

延迟一律从 system.query_log 取（服务端耗时，按 log_comment 区分每组），不含 docker exec 与网络开销。
"""

from __future__ import annotations

import argparse
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

DC = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "exec", "-T", "clickhouse"]
AUTH = ["-u", os.getenv("CH_USER", "salestrend"), "--password", os.getenv("CH_PASSWORD", "salestrend")]
PRESET = ["UTC", "Asia/Shanghai", "America/Los_Angeles", "America/New_York", "Europe/London", "Asia/Kolkata"]
PLATFORMS = ["app", "marketplace", "website"]
CLS, RV = "maxi-dress", "p5"

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
SELECT 'maxi-dress', 'p5', toUInt32(number + 1), if((number + 1) % 10 = 0, 'match', 'exclude'),
       if((number + 1) % 10 = 0, 'P5_match', 'P5_other'), [], now64(3, 'UTC')
FROM numbers(5000)
"""


def ch(sql: str, db: str, comment: str = "", fmt: str = "TSV", stdin: str | None = None) -> str:
    args = [*DC, "clickhouse-client", *AUTH, "--database", db, "--format", fmt]
    if comment:
        args += ["--log_comment", comment]
    if stdin is not None:
        args += ["--multiquery"]
    else:
        args += ["-q", sql]
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
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


def e2e(db: str, out: Path, rates: list[int], duration: str) -> None:
    """另起一个指向 p5 库的 API 实例（同一镜像，只改 CH_DATABASE），k6 压一年区间小时表段。

    缓存旁路：REDIS_URL 指向一个拒绝连接的端口。ResponseCache 把 Redis 故障一律当未命中（设计如此），
    连接被拒是立即返回的，不会吃 0.3s 超时；每个请求都真打存储。端到端数字因此是「全部未命中」的最坏情况。
    """
    # API 只认 rules/ 下的规则版本（v1–v3），把 p5 的成员原样复制一份到 v1 下，默认版本即可查
    ch("INSERT INTO product_class_member SELECT class_slug, 'v1', product_id, decision, matched_rule, warnings, "
       "classified_at FROM product_class_member FINAL WHERE rule_version = 'p5'", db)
    api = docker("compose", "-f", str(ROOT / "docker-compose.yml"), "ps", "-q", "api")
    image = docker("inspect", "-f", "{{.Config.Image}}", api)
    net = docker("inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}", api)
    name = "sales-trend-api-p5"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--name", name, "--network", net, "-p", "8001:8000", "-e", "CH_HOST=clickhouse",
           "-e", f"CH_DATABASE={db}", "-e", f"CH_USER={AUTH[1]}", "-e", f"CH_PASSWORD={AUTH[3]}",
           "-e", "REDIS_URL=redis://127.0.0.1:1/0", "-e", "RATE_LIMIT_PER_S=100000", "-e", "RATE_LIMIT_BURST=100000",
           image)
    try:
        for _ in range(60):
            if subprocess.run(["curl", "-sf", "http://localhost:8001/healthz"], capture_output=True,
                              check=False).returncode == 0:
                break
            time.sleep(1)
        for rate in rates:
            subprocess.run(["k6", "run", "--quiet", "-e", "SCEN=p5", "-e", "API=http://localhost:8001",
                            "-e", f"RATE={rate}", "-e", f"DURATION={duration}",
                            "--summary-export", str(out / f"p5_e2e_r{rate}.json"), str(ROOT / "perf/k6/scenarios.js")],
                           capture_output=True, check=False)
            print("e2e rate", rate, "done", flush=True)
    finally:
        docker("rm", "-f", name, check=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--db", default="p5")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-load", action="store_true", help="库已建好，只重跑查询")
    ap.add_argument("--skip-queries", action="store_true", help="只跑端到端")
    ap.add_argument("--e2e-rates", default="2,5,10", help="端到端 k6 到达率（RPS），逗号分隔；空串跳过端到端")
    ap.add_argument("--e2e-duration", default="60s")
    a = ap.parse_args()
    db, run_id = a.db, datetime.now(UTC).strftime("%H%M%S")
    out = ROOT / "perf" / "results"
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {"rows_requested": a.rows, "db": db}
    if a.skip_load and (out / "p5.json").exists():  # 只重跑查询时保留上次的写入与重算耗时
        prev = json.loads((out / "p5.json").read_text())
        keep = ("rows_requested", "load", "rollup_hourly", "rollup_daily") + (("queries",) if a.skip_queries else ())
        res.update({k: prev[k] for k in keep if k in prev})

    if not a.skip_load:
        ch(f"DROP DATABASE IF EXISTS {db}", "default")
        ch(f"CREATE DATABASE {db}", "default")
        ch("", db, stdin=(ROOT / "sql" / "ddl" / "001_tables.sql").read_text())
        print("ddl ok", flush=True)
        res["load"] = timed(db, f"p5-{run_id}-load", [GENERATE.format(rows=a.rows).strip(), MEMBERS.strip()])
        print("load", res["load"], flush=True)
        wait_merges(db)

        # 小时表重算：与 store.rollup 同一份 SQL，区间覆盖 2024 全年
        ver = int(time.time() * 1000)
        res["rollup_hourly"] = timed(db, f"p5-{run_id}-rollup-hourly", render("rollup/hourly.sql", {
            "from": datetime(2024, 1, 1, tzinfo=UTC), "to": datetime(2025, 1, 1, tzinfo=UTC), "version": ver}))
        print("rollup hourly", res["rollup_hourly"], flush=True)
        # 品类日表重算：一个（品类, 规则版本, 预设时区）一次，6 个预设时区逐个计时
        res["rollup_daily"] = {}
        for tz in PRESET:
            f, t = utc_range(date(2024, 1, 1), date(2024, 12, 31), tz)
            res["rollup_daily"][tz] = timed(db, f"p5-{run_id}-rollup-daily-{tz}", render("rollup/class_daily.sql", {
                "cls": CLS, "rv": RV, "tz": tz, "d1": date(2024, 1, 1), "d2": date(2024, 12, 31),
                "from": f, "to": t, "version": ver}))
            print("rollup daily", tz, res["rollup_daily"][tz], flush=True)
        # 水位：模拟「2024 年已全部封口」，供指向 p5 的 API 实例做端到端压测
        mx = ch("SELECT max(event_ts) FROM fact_sales", db)
        rows = [("sealed_until", "2025-01-01 00:00:00"), ("data_through", mx)]
        rows += [(f"data_through:{s}", ch(f"SELECT max(event_ts) FROM fact_sales WHERE source = '{s}'", db))
                 for s in PLATFORMS]
        ch("INSERT INTO pipeline_state VALUES " + ",".join(f"('{k}', '{v}', now64(3))" for k, v in rows), db)
        wait_merges(db)

    res["storage"] = json.loads(ch(f"""
        SELECT table, sum(rows) AS rows, sum(data_uncompressed_bytes) AS raw_bytes,
               sum(data_compressed_bytes) AS bytes, round(raw_bytes / bytes, 2) AS ratio, count() AS parts
        FROM system.parts WHERE database = '{db}' AND active GROUP BY table ORDER BY bytes DESC""",
        db, fmt="JSON"))["data"]
    print("storage", res["storage"], flush=True)

    # 三段查询：日表一年（预设 UTC）、小时表一年（非预设整点 Europe/Berlin）、明细 31 天（半点 Asia/Kathmandu）
    hs, he = utc_range(date(2024, 1, 1), date(2024, 12, 31), "Europe/Berlin")
    fs, fe = utc_range(date(2024, 6, 1), date(2024, 7, 1), "Asia/Kathmandu")
    base = {"cls": CLS, "rv": RV, "platforms": PLATFORMS}
    queries = {
        "class_daily": render("queries/trend_daily.sql", {**base, "tz": "UTC", "d1": date(2024, 1, 1),
                                                           "d2": date(2024, 12, 31)})[0],
        "hourly": render("queries/trend_hourly.sql", {**base, "tz": "Europe/Berlin", "s": hs, "e": he})[0],
        "fact": render("queries/trend_fact.sql", {**base, "tz": "Asia/Kathmandu", "s": fs, "e": fe})[0],
    }
    for name, sql in ({} if a.skip_queries else queries).items():
        res.setdefault("queries", {})
        variants = {"final": sql, "nofinal": sql.replace(" FINAL", "")}
        # 先确认去掉 FINAL 结果不变：结果一样才谈得上「FINAL 是不是多余」
        sums = {k: ch(f"SELECT sum(sales), count() FROM ({v})", db) for k, v in variants.items()}
        q = {"sql": sql, "same_result": sums["final"] == sums["nofinal"], "result": sums["final"]}
        for k, v in variants.items():
            ch("", db, comment=f"p5-{run_id}-warm", stdin=";\n".join([v] * 3) + ";")  # 预热页缓存
            tag = f"p5-{run_id}-{name}-{k}-serial"
            ch("", db, comment=tag, stdin=";\n".join([v] * a.runs) + ";")
            q[f"{k}_serial"] = log_stats(db, tag)
            q[f"{k}_c8"] = bench(db, f"p5-{run_id}-{name}-{k}-c8", v, 8, max(a.runs * 2, 120))
            print(name, k, q[f"{k}_serial"], q[f"{k}_c8"], flush=True)
        # 并发拐点：只测带 FINAL 的主路径
        q["curve"] = [bench(db, f"p5-{run_id}-{name}-c{c}", sql, c, max(40, c * 10)) for c in (1, 2, 4, 16)]
        res["queries"][name] = q
    (out / "p5.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out / 'p5.json'}")
    if a.e2e_rates:
        e2e(db, out, [int(x) for x in a.e2e_rates.split(",")], a.e2e_duration)
    if not a.keep:
        ch(f"DROP DATABASE {db}", "default")
        print(f"dropped {db}")


if __name__ == "__main__":
    main()
