"""把 k6 --summary-export 的结果汇总成 docs/perf-report.md。"""

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
        "## 结果",
        "",
        *table,
        "",
        f"**P3 击穿**：500 个并发请求打同一个冷 key，存储层实际收到的趋势查询 **{herd_q} 次**。"
        "singleflight 是进程内的，API 起了 2 个 worker，所以上限是 2 次；没有 singleflight 时是 500 次。"
        "多实例部署要做到全局 1 次，需要把锁放到 Redis（`SET NX` + 短 TTL），代价是多一次网络往返。",
        "",
        f"**P4 降级**：停掉 ClickHouse 后，已缓存过的 key 返回 **{deg.get('cached_key_status', '?')}**"
        f"（`{deg.get('cached_key_meta', '?')}`，带 `Warning: 110` 响应头）；从没缓存过的 key 返回 "
        f"**{deg.get('uncached_key_status', '?')} {deg.get('uncached_key_code', '')}**（RFC 9457 错误体）。",
        "",
        "## 解读",
        "",
        "- 样例数据只有 300 行，P2 的存储耗时几乎全是固定开销（HTTP、序列化、ClickHouse 查询解析），"
        "不能代表大数据量下的表现。大数据量的验证（P5：合成千万级明细跑一年区间小时表查询）这次没有做，见 README「已知局限」。",
        "- 生产 SLO（2000 QPS，P99 ≤ 200 ms）的推导与本机目标分开写，见设计说明。本机数字只用于证明方案在单机上的量级和缓存的作用。",
        "",
    ]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
