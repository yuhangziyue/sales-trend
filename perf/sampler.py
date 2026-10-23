"""k6 压测时顺手采 CPU：容器侧用 docker stats，宿主侧用 ps 看 k6 进程。用来判断拐点处瓶颈在 API、ClickHouse 还是 k6。

docker stats 的 CPU% 以「1 个核 = 100%」计，colima 虚拟机 4 vCPU，所以容器加起来上限约 400%。
k6 跑在宿主机上（不在虚拟机里），它的 CPU% 同样以 1 核 = 100% 计。
采样从压测进行到 1/3 开始，间隔约 2 秒（docker stats --no-stream 本身要 1–2 秒），取平均。
"""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path


def _docker_cpu() -> dict[str, float]:
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"],
                       capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=30)
    out = {}
    for ln in r.stdout.splitlines():
        name, _, pct = ln.partition("\t")
        if pct.endswith("%"):
            out[name] = float(pct[:-1])
    return out


def _ps_cpu(pid: int) -> float | None:
    r = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)], capture_output=True, text=True, check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def run_k6(args: list[str], summary: Path, duration_s: float) -> dict:
    """跑一档 k6（args 不含 --summary-export），返回 {p50, p95, p99, failed, reqs, cpu: {...}}，并把 CPU 写进旁边的 .cpu.json。"""
    proc = subprocess.Popen(["k6", "run", "--quiet", "--summary-export", str(summary), *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    time.sleep(duration_s / 3)
    samples: list[dict[str, float]] = []
    k6_cpu: list[float] = []
    while proc.poll() is None and len(samples) < 8:
        if (c := _ps_cpu(proc.pid)) is not None:
            k6_cpu.append(c)
        samples.append(_docker_cpu())
        time.sleep(1)
    proc.wait()
    names = {n for s in samples for n in s}
    cpu = {n: round(statistics.mean(s.get(n, 0.0) for s in samples), 1) for n in sorted(names)}
    cpu["k6(host)"] = round(statistics.mean(k6_cpu), 1) if k6_cpu else None
    summary.with_suffix(".cpu.json").write_text(json.dumps(cpu))
    m = json.loads(summary.read_text())["metrics"]
    d = m["http_req_duration"]
    return {"p50": d["med"], "p95": d["p(95)"], "p99": d["p(99)"], "failed": m["http_req_failed"]["value"],
            "reqs": m["http_reqs"]["count"], "rate": m["http_reqs"]["rate"], "cpu": cpu}
