"""ClickHouse 访问层：建表、写入、汇总重算、水位、三段查询。"""

from __future__ import annotations

import time as _time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import clickhouse_connect

from .classify.rules import RuleSet, normalize
from .ingest.pipeline import REJECT_FLAGS, Event, clean
from .metrics import STORAGE_QUERIES
from .query.planner import Plan, local_day_utc
from .settings import ROOT, Settings

SQL = ROOT / "sql"


def _statements(path: Path) -> list[str]:
    lines = [ln for ln in path.read_text().splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _sql(path: str) -> str:
    return _statements(SQL / path)[0]


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Store:
    def __init__(self, s: Settings, client=None):
        self.s = s
        self.client = client or clickhouse_connect.get_client(
            host=s.ch_host, port=s.ch_port, username=s.ch_user, password=s.ch_password,
            database=s.ch_database, connect_timeout=2,
            # 不开会话：clickhouse-connect 默认每个 client 一个 session，同一 session 不许并发查询，
            # API 用线程池共享一个 client 时 200 个并发里 197 个报错（压测 P1 实测）。查询本来就无状态
            autogenerate_session_id=False,
            settings={"max_execution_time": int(max(1, s.query_timeout_s))},
        )

    # ---------- 建表 ----------
    def reset(self) -> None:
        for t in ("raw_sales_events", "fact_sales", "dim_product", "product_class_member",
                  "agg_product_hourly", "agg_class_daily", "pipeline_state"):
            self.client.command(f"DROP TABLE IF EXISTS {t}")
        self.migrate()

    def migrate(self) -> None:
        for stmt in _statements(SQL / "ddl" / "001_tables.sql"):
            self.client.command(stmt)

    def ping(self) -> bool:
        return self.client.command("SELECT 1") == 1

    # ---------- 写入 ----------
    def known_keys(self, events: list[Event]) -> set[str]:
        """跨批去重：只查本批时间窗内 fact 已有的 key，不全表扫。"""
        ts = [e.event_ts for e in events if e.event_ts]
        if not ts:
            return set()
        rows = self.client.query(
            "SELECT DISTINCT event_key FROM fact_sales WHERE event_ts >= {a:DateTime64(3,'UTC')} "
            "AND event_ts <= {b:DateTime64(3,'UTC')}",
            parameters={"a": min(ts), "b": max(ts)},
        ).result_rows
        return {r[0] for r in rows}

    def write_events(self, events: list[Event]) -> str:
        batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        now = datetime.now(UTC)
        raw_cols = ["event_key", "batch_id", "row_no", "source", "product_id", "title_raw", "sales_count",
                    "ts_raw", "source_tz", "event_ts", "user_id", "category_raw", "is_duplicate", "dup_of",
                    "dq_flags", "ingested_at"]
        self.client.insert("raw_sales_events", [
            [e.event_key, batch_id, e.row_no, e.source, e.product_id, e.title_raw, e.sales_count, e.ts_raw,
             e.source_tz, e.event_ts, e.user_id, e.category_raw, int(e.is_duplicate), e.dup_of, e.dq_flags, now]
            for e in events], column_names=raw_cols)
        good = clean(events)
        if good:
            self.client.insert("fact_sales", [
                [e.event_key, e.product_id, e.source, e.sales_count, e.event_ts, now] for e in good],
                column_names=["event_key", "product_id", "source", "sales_count", "event_ts", "ingested_at"])
        return batch_id

    def refresh_dim_product(self) -> None:
        """维表从 fact 全量重建：一个 id 汇总它见过的全部标题写法。"""
        rows = self.client.query(
            "SELECT assumeNotNull(product_id), title_raw, any(category_raw), count() FROM raw_sales_events "
            "WHERE is_duplicate = 0 AND NOT hasAny(dq_flags, {rejects:Array(String)}) "
            "GROUP BY product_id, title_raw",
            parameters={"rejects": sorted(REJECT_FLAGS)},
        ).result_rows
        by_id: dict[int, dict] = defaultdict(lambda: {"titles": Counter(), "cat": Counter()})
        for pid, title, cat, n in rows:
            by_id[pid]["titles"][title] += n
            by_id[pid]["cat"][cat] += n
        now = datetime.now(UTC)
        out = []
        for pid, d in sorted(by_id.items()):
            norm = Counter()
            for t, n in d["titles"].items():
                norm[normalize(t)] += n
            canonical = norm.most_common(1)[0][0]
            out.append([pid, canonical, sorted(set(canonical.split())), sorted(d["titles"]),
                        d["cat"].most_common(1)[0][0], now])
        self.client.insert("dim_product", out, column_names=[
            "product_id", "canonical_title", "title_tokens", "title_variants", "category_raw", "updated_at"])

    def products(self) -> list[tuple[int, list[str], str]]:
        return [(r[0], list(r[1]), r[2]) for r in self.client.query(
            "SELECT product_id, title_variants, category_raw FROM dim_product FINAL ORDER BY product_id"
        ).result_rows]

    def classify(self, rulesets: dict[tuple[str, str], RuleSet]) -> dict[tuple[str, str], dict[int, object]]:
        now = datetime.now(UTC)
        prods = self.products()
        result = {}
        for key, rs in rulesets.items():
            decisions = {pid: rs.classify_product(titles, cat) for pid, titles, cat in prods}
            result[key] = decisions
            self.client.insert("product_class_member", [
                [rs.class_slug, rs.rule_version, pid, d.decision, d.rule_id, list(d.warnings), now]
                for pid, d in decisions.items()], column_names=[
                "class_slug", "rule_version", "product_id", "decision", "matched_rule", "warnings",
                "classified_at"])
        return result

    # ---------- 汇总重算 ----------
    def rollup(self, rulesets: dict[tuple[str, str], RuleSet], now: datetime | None = None) -> dict:
        """sealed_until = 当前时刻向下取整点。演示数据是历史数据，用「数据最新时间」模拟当前时刻。"""
        lo, through = self.client.query("SELECT min(event_ts), max(event_ts) FROM fact_sales").result_rows[0]
        through, lo = _utc(through), _utc(lo)
        now = now or through
        sealed = now.replace(minute=0, second=0, microsecond=0)
        version = int(_time.time() * 1000)

        start = lo.replace(minute=0, second=0, microsecond=0)
        self._run(SQL / "rollup" / "hourly.sql", {"from": start, "to": sealed, "version": version})

        daily = 0
        for (cls, rv) in rulesets:
            for tz_name in self.s.preset_timezones:
                tz = ZoneInfo(tz_name)
                d1 = lo.astimezone(tz).date()
                d2 = d1
                while local_day_utc(d2 + timedelta(1), tz)[1] <= sealed:
                    d2 += timedelta(1)
                if local_day_utc(d2, tz)[1] > sealed:
                    continue
                f, t = local_day_utc(d1, tz)[0], local_day_utc(d2, tz)[1]
                self._run(SQL / "rollup" / "class_daily.sql", {
                    "cls": cls, "rv": rv, "tz": tz_name, "d1": d1, "d2": d2, "from": f, "to": t,
                    "version": version})
                daily += 1
        self.set_state({"sealed_until": sealed, "data_through": through, **{
            f"data_through:{src}": _utc(ts) for src, ts in self.client.query(
                "SELECT source, max(event_ts) FROM fact_sales GROUP BY source").result_rows}})
        return {"sealed_until": sealed, "data_through": through, "class_daily_jobs": daily, "version": version}

    def _run(self, path: Path, params: dict) -> None:
        for stmt in _statements(path):
            self.client.command(stmt, parameters=params)

    # ---------- 水位 ----------
    def set_state(self, kv: dict[str, datetime]) -> None:
        now = datetime.now(UTC)
        self.client.insert("pipeline_state", [[k, v, now] for k, v in kv.items()],
                           column_names=["key", "value", "updated_at"])

    def state(self) -> dict[str, datetime]:
        return {k: _utc(v) for k, v in self.client.query(
            "SELECT key, value FROM pipeline_state FINAL").result_rows}

    # ---------- 查询 ----------
    def run_plan(self, plan: Plan, cls: str, rv: str, tz_name: str,
                 platforms: list[str]) -> dict[tuple[date, str], int]:
        tz = ZoneInfo(tz_name)
        out: dict[tuple[date, str], int] = defaultdict(int)
        for seg in plan.segments:
            s, e = seg.utc_range(tz)
            params = {"cls": cls, "rv": rv, "tz": tz_name, "platforms": platforms,
                      "d1": seg.first, "d2": seg.last, "s": s, "e": e}
            sql = {"class_daily": "queries/trend_daily.sql", "hourly": "queries/trend_hourly.sql",
                   "fact": "queries/trend_fact.sql"}[seg.segment]
            rows = self.client.query(_sql(sql), parameters=params).result_rows
            STORAGE_QUERIES.labels(seg.segment).inc()  # 回源率的分子：查成功一段记一段
            for local_date, source, sales in rows:
                if seg.first <= local_date <= seg.last:
                    out[(local_date, source)] += int(sales)
        return out
