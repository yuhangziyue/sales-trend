"""容量：缓存命中路径的单机拐点，以及 API worker 数的对照（2 个 vs 4 个）。

用法：uv run python perf/capacity.py [--workers 2,4] [--rates 500,1000,2000,3000,4000,6000,8000]
      结果写 perf/results/capacity_hit_w{W}_r{RATE}.json（及 .cpu.json），由 perf/report.py 汇总。

在样例库上测（命中路径与数据量无关）：每个 worker 数各重建一次 api 容器（API_WORKERS 环境变量），
放开限流，先把 20 个热点 key 预热进 Redis，再按固定到达率逐档加压，每档 30 秒。
P99 超过目标（20 ms）的 2 倍或失败率超过 1% 就停。测完按默认配置重建 api 容器。
未命中的三条路径（日表段、品类 15 分钟表段、实时）在 P5 库上测，见 perf/p5.py 的端到端。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "perf"))
from sampler import run_k6

TARGET_MS = 20
DC = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]


def api_up(env: dict[str, str]) -> None:
    subprocess.run([*DC, "up", "-d", "--wait", "api"], env={**os.environ, **env}, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)
    time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", default="2,4")
    ap.add_argument("--rates", default="500,1000,2000,3000,4000,6000,8000")
    ap.add_argument("--duration", default="30s")
    ap.add_argument("--api", default=f"http://localhost:{os.getenv('API_HOST_PORT', '8000')}")
    a = ap.parse_args()
    out = ROOT / "perf" / "results"
    out.mkdir(parents=True, exist_ok=True)
    k6 = ["-e", "SCEN=hit", "-e", f"API={a.api}", "-e", f"DURATION={a.duration}", str(ROOT / "perf/k6/scenarios.js")]
    try:
        for w in [int(x) for x in a.workers.split(",")]:
            for f in out.glob(f"capacity_hit_w{w}_r*.json"):
                f.unlink()
            api_up({"API_WORKERS": str(w), "RATE_LIMIT_PER_S": "1000000", "RATE_LIMIT_BURST": "1000000"})
            # 预热：20 个热点 key 各打几次，之后全部命中
            subprocess.run(["k6", "run", "--quiet", "-e", "SCEN=hot", "-e", f"API={a.api}", "--iterations", "400",
                            "--vus", "20", str(ROOT / "perf/k6/scenarios.js")], capture_output=True, check=False,
                           stdin=subprocess.DEVNULL)
            for rate in [int(x) for x in a.rates.split(",")]:
                r = run_k6(["-e", f"RATE={rate}", *k6], out / f"capacity_hit_w{w}_r{rate}.json",
                           float(a.duration.rstrip("s")))
                print("hit", w, rate, {k: round(r[k], 2) for k in ("p50", "p99", "failed", "rate")}, r["cpu"], flush=True)
                if r["p99"] > 2 * TARGET_MS or r["failed"] > 0.01:
                    break
    finally:
        api_up({})  # 回到默认配置（默认 worker 数与限流）


if __name__ == "__main__":
    main()
