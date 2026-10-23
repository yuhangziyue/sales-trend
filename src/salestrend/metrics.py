"""Prometheus 指标。

API 是 uvicorn 多 worker：每个 worker 是独立进程，各数各的。所以生产必须开 prometheus_client 多进程模式——
设 PROMETHEUS_MULTIPROC_DIR（Dockerfile 里设，启动时清空），每个 worker 把计数写进该目录下的 mmap 文件，
/metrics 用 MultiProcessCollector 把所有 worker 的文件加起来。不开的话，/metrics 落到哪个 worker
就只返回哪个 worker 的数，命中率、请求总数都是错的。环境变量必须在 import prometheus_client 之前就设好。

没设这个变量时（单元测试、本地单进程）退回进程内默认注册表。

命中率分三档：第一层 hit、月块全命中 block、部分命中 block_partial，分母都是 hit + block + block_partial + miss + stale；
回源率 = storage_queries_total / trend_requests_total。公式与告警线见 design.md §7.2。
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

# 1ms 到 5s：缓存命中在 1–3ms，汇总段几毫秒到几十毫秒，明细段可到秒级，超时线 2s
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

TREND_REQUESTS = Counter(
    "trend_requests_total", "趋势接口请求数。cache=hit|block|block_partial|miss|stale；"
    "none = 没走到缓存（参数错、限流、404）",
    ["cache", "status"])
TREND_SECONDS = Histogram(
    "trend_request_seconds", "趋势接口耗时（成功响应）。"
    "path=cache_hit|block|block_partial|stale|class_daily|class_15m|fact|mixed|empty",
    ["path"], buckets=LATENCY_BUCKETS)
STORAGE_QUERIES = Counter("storage_queries_total", "实际执行的分段 SQL 数，每段一次", ["segment"])
SINGLEFLIGHT_SHARED = Counter("singleflight_shared_total", "被 singleflight 合并、等别人结果的请求数")
# max：任一 worker 熔断即为 1。熔断按时间自动恢复，没有事件可挂，所以在每次判断状态时回写；
# 空闲 worker 会保留最后一次的值，直到下一个请求落到它身上
CIRCUIT_OPEN = Gauge("circuit_open", "存储熔断状态：1 = 打开（只读缓存，不打存储）", multiprocess_mode="max")


def trend_path(meta: dict) -> str:
    """延迟按「这次请求实际读了什么」分桶：命中缓存、单段、或多段混合。"""
    if meta.get("cache") == "hit":
        return "cache_hit"
    if meta.get("cache") in ("stale", "block", "block_partial"):
        return meta["cache"]  # 月块路径单独分桶：拼装的代价要能和第一层命中、直接查库分开看
    segs = {p["segment"] for p in meta.get("plan", [])}
    if not segs:
        return "empty"  # 区间全部晚于数据截止：不查库，直接返回 null
    return segs.pop() if len(segs) == 1 else "mixed"


def render() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # 汇总目录下所有 worker 的文件
    else:
        registry = REGISTRY
    return generate_latest(registry), CONTENT_TYPE_LATEST
