"""写入管线（纯函数部分）：解析 → 规范化 → event_key → 质量校验 → 去重。

全部脏活在进 ClickHouse 之前做完：ClickHouse 的去重是最终一致的，重复行一旦进了汇总就撤不回来，
所以「去重必须发生在聚合之前」。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

# 拒收：只进 raw，不进 fact
REJECT_FLAGS = {"bad_product_id", "bad_sales_count", "bad_timestamp", "negative_sales", "unknown_source"}


@dataclass
class Event:
    row_no: int
    product_id: int | None
    title_raw: str
    sales_count: int | None
    ts_raw: str
    source: str
    source_tz: str
    event_ts: datetime | None  # UTC, tz-aware
    user_id: str
    category_raw: str
    event_key: str = ""
    dq_flags: list[str] = field(default_factory=list)
    is_duplicate: bool = False
    dup_of: str = ""

    @property
    def rejected(self) -> bool:
        return bool(REJECT_FLAGS & set(self.dq_flags))


def read_xlsx(path: Path) -> list[dict]:
    ws = openpyxl.load_workbook(path, read_only=True)["sample_sales_data"]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    return [dict(zip(header, r, strict=False)) for r in rows if any(v is not None for v in r)]


def _ts_text(v) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return "" if v is None else str(v).strip()


def parse_row(row_no: int, row: dict, platforms: dict[str, str]) -> Event:
    flags: list[str] = []
    source = str(row.get("source") or "").strip().lower()
    source_tz = platforms.get(source, "UTC")
    if source not in platforms:
        flags.append("unknown_source")

    try:
        product_id = int(row.get("product_id"))
    except (TypeError, ValueError):
        product_id, flags = None, [*flags, "bad_product_id"]

    try:
        sales = int(row.get("sales_count"))
        if sales < 0:
            flags.append("negative_sales")
    except (TypeError, ValueError):
        sales, flags = None, [*flags, "bad_sales_count"]

    ts_raw = _ts_text(row.get("timestamp"))
    try:
        naive = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
        # 无时区时间按平台源时区解释，再统一转成 UTC 存
        event_ts = naive.replace(tzinfo=ZoneInfo(source_tz)).astimezone(UTC)
    except ValueError:
        event_ts, flags = None, [*flags, "bad_timestamp"]

    return Event(
        row_no=row_no,
        product_id=product_id,
        title_raw=str(row.get("title") or ""),
        sales_count=sales,
        ts_raw=ts_raw,
        source=source,
        source_tz=source_tz,
        event_ts=event_ts,
        user_id=str(row.get("user_id") or "").strip(),
        category_raw=str(row.get("category") or "").strip().lower(),
        dq_flags=flags,
    )


def event_key(e: Event) -> str:
    """七个原始字段规范化后拼接做 sha256。

    只做「不改变语义」的规范化（去首尾空白、平台与类目小写），标题不做归一化：
    'Red Maxi Dress' 与 'Red Maxi-Dress' 是两条不同的上报，不能被当成重复。
    user_id 只是模拟字段、不是订单号，所以不能单独当幂等键，只作为七字段之一。
    """
    parts = [
        str(e.product_id),
        e.title_raw.strip(),
        str(e.sales_count),
        e.ts_raw,
        e.user_id,
        e.category_raw,
        e.source,
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def quality_flags(events: list[Event], now: datetime) -> None:
    """告警与信息级标记（拒收级在 parse_row 里已经打上）。"""
    by_product: dict[int, list[int]] = defaultdict(list)
    for e in events:
        if e.product_id is not None and e.sales_count is not None and not e.rejected:
            by_product[e.product_id].append(e.sales_count)
    sorted_vals = {pid: sorted(v) for pid, v in by_product.items()}

    def p99_of_others(pid: int, v: int) -> int:
        # 留一法：拿「同商品的其他行」做基线，否则离群值会把自己所在批次的 P99 拉高，永远判不出来
        others = list(sorted_vals.get(pid, []))
        if v in others:
            others.remove(v)
        return others[min(len(others) - 1, int(len(others) * 0.99))] if others else 0

    for e in events:
        if e.rejected:
            continue
        if e.sales_count and e.sales_count > 10 * max(p99_of_others(e.product_id, e.sales_count), 1):
            e.dq_flags.append("sales_outlier")
        if e.event_ts and e.event_ts > now:
            e.dq_flags.append("future_ts")
        local_t = datetime.strptime(e.ts_raw, "%Y-%m-%d %H:%M:%S").time()
        if local_t < time(0, 30) or local_t >= time(23, 30):
            e.dq_flags.append("day_boundary")  # 日界敏感行：换时区后会换日


def dedupe(events: list[Event], known_keys: set[str] | None = None) -> None:
    """批内去重 + 跨批去重（known_keys 来自 fact 表里同时间窗已有的 key）。只标不删。"""
    seen: dict[str, int] = {}
    known_keys = known_keys or set()
    for e in events:
        if e.rejected:
            continue
        if e.event_key in known_keys:
            e.is_duplicate, e.dup_of = True, e.event_key
            e.dq_flags.append("duplicate_cross_batch")
        elif e.event_key in seen:
            e.is_duplicate, e.dup_of = True, e.event_key
            e.dq_flags.append("duplicate")
        else:
            seen[e.event_key] = e.row_no


def process(rows: list[dict], platforms: dict[str, str], now: datetime | None = None,
            known_keys: set[str] | None = None) -> list[Event]:
    now = now or datetime.now(UTC)
    events = [parse_row(i + 2, r, platforms) for i, r in enumerate(rows)]  # +2：xlsx 行号，跳过表头
    for e in events:
        e.event_key = event_key(e)
    quality_flags(events, now)
    dedupe(events, known_keys)
    return events


def clean(events: list[Event]) -> list[Event]:
    return [e for e in events if not e.rejected and not e.is_duplicate]
