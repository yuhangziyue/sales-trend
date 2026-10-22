"""FastAPI 入口。品类是资源：/v1/product-classes/{class_slug}/sales-trend，maxi-dress 只是一个实例。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from ..cache.resilience import ResponseCache, TokenBucket
from ..classify.rules import default_versions, load_rulesets
from ..metrics import TREND_REQUESTS, TREND_SECONDS, render, trend_path
from ..service import TrendService
from ..settings import Settings, get_settings
from .errors import Problem, problem_handler
from .params import parse_trend_params
from .schemas import TrendResponse

log = logging.getLogger("salestrend")


def create_app(settings: Settings | None = None, store=None, cache: ResponseCache | None = None,
               use_redis: bool = True) -> FastAPI:
    s = settings or get_settings()
    rulesets = load_rulesets(s.rules_dir)        # 品类列表进程内常驻：未知 slug 直接 404，不碰存储（防穿透）
    defaults = default_versions(rulesets)

    app = FastAPI(title="Sales Trend API", version="1.0.0", 
                  description="多平台销量趋势查询。错误体遵循 RFC 9457（application/problem+json）。")
    app.add_exception_handler(Problem, problem_handler)
    if cache is None and use_redis:
        cache = ResponseCache(s.redis_url)
    def store_factory():
        from ..store import Store  # 延迟导入并按需连接：ClickHouse 晚于 API 启动也能自愈

        return Store(s)

    app.state.service = TrendService(s, store, cache, store_factory)
    bucket = TokenBucket(s.rate_per_s, s.rate_burst)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = "req_" + uuid.uuid4().hex[:16]
        resp = await call_next(request)
        resp.headers["X-Request-ID"] = request.state.request_id
        return resp

    def _class(slug: str):
        versions = sorted(v for (c, v) in rulesets if c == slug)
        if not versions:
            raise Problem("CLASS_NOT_FOUND", f"product class '{slug}' does not exist",
                          [{"name": "class_slug", "reason": f"known: {', '.join(sorted(defaults))}"}])
        return versions

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz():
        svc = app.state.service
        ch = await _safe(lambda: svc.get_store().ping())
        rd = await svc.cache.ping() if svc.cache else False
        body = {"clickhouse": ch, "redis": rd}
        return JSONResponse(body, status_code=200 if ch else 503)

    @app.get("/metrics", include_in_schema=False)  # 不进 OpenAPI 业务部分；不走限流：抓取方被 429 等于监控失明
    async def metrics():
        body, content_type = render()
        return Response(body, media_type=content_type)

    @app.get("/v1/product-classes", tags=["classes"])
    async def list_classes():
        return {"data": [{"class_slug": c, "default_rule_version": v, "rule_versions": _class(c),
                          "description": rulesets[(c, v)].description} for c, v in sorted(defaults.items())]}

    @app.get("/v1/product-classes/{class_slug}", tags=["classes"])
    async def get_class(class_slug: str):
        versions = _class(class_slug)
        rs = rulesets[(class_slug, defaults[class_slug])]
        return {"data": {"class_slug": class_slug, "default_rule_version": rs.rule_version,
                         "rule_versions": versions, "description": rs.description,
                         "normalize": rs.normalize_steps, "rules": rs.rules, "source_file": rs.source_file}}

    @app.get("/v1/meta/freshness", tags=["meta"])
    async def freshness():
        st = await app.state.service.pipeline_state()
        return {"data": {k: v.strftime("%Y-%m-%dT%H:%M:%SZ") for k, v in sorted(st.items())}}

    @app.get("/v1/product-classes/{class_slug}/sales-trend", tags=["trend"], responses={
        200: {"model": TrendResponse, "description": "列式趋势。week / month 额外带 days_in_bucket、days_covered、"
                                                      "partial、daily_avg 四个平行数组；day 粒度不带"}})
    async def sales_trend(
        request: Request,
        class_slug: str,
        start_date: str | None = Query(None, description="本地日期，闭区间起点，YYYY-MM-DD"),
        end_date: str | None = Query(None, description="本地日期，闭区间终点（包含），YYYY-MM-DD"),
        platforms: str | None = Query(None, description="逗号分隔，如 app,website；默认全部"),
        tz: str | None = Query(None, description="IANA 时区名，默认 UTC；不接受 +08:00 这类固定偏移"),
        granularity: str | None = Query(None, description="day | week | month，默认 day"),
        rule_version: str | None = Query(None, description="品类规则版本，默认当前版本"),
    ):
        t0 = time.perf_counter()
        try:
            resp, meta = await _trend(request, class_slug, start_date, end_date, platforms, tz, granularity,
                                      rule_version)
        except Problem as e:
            # 503 / 504 是缓存未命中后存储失败，算 miss（命中率分母要含它）；其余错误没走到缓存，记 none
            cache = "miss" if e.code in ("UPSTREAM_UNAVAILABLE", "QUERY_TIMEOUT") else "none"
            TREND_REQUESTS.labels(cache, str(e.status)).inc()
            raise
        TREND_REQUESTS.labels(meta["cache"], str(resp.status_code)).inc()
        TREND_SECONDS.labels(trend_path(meta)).observe(time.perf_counter() - t0)
        return resp

    async def _trend(request: Request, class_slug, start_date, end_date, platforms, tz, granularity,
                     rule_version) -> tuple[Response, dict]:
        api_key = request.headers.get("X-API-Key") or (request.client.host if request.client else "anon")
        if wait := bucket.take(api_key):
            raise Problem("RATE_LIMITED", "rate limit exceeded", headers={"Retry-After": str(max(1, round(wait)))})

        versions = _class(class_slug)
        rv = rule_version or defaults[class_slug]
        if rv not in versions:
            raise Problem("RULE_VERSION_NOT_FOUND", f"rule_version '{rv}' not found for '{class_slug}'",
                          [{"name": "rule_version", "reason": f"known: {', '.join(versions)}"}])
        p = parse_trend_params(class_slug=class_slug, rule_version=rv, start_date=start_date, end_date=end_date,
                               platforms=platforms, tz=tz, granularity=granularity,
                               known_platforms=list(app.state.service.s.platforms),
                               max_days=app.state.service.s.max_range_days)

        body, ttl = await app.state.service.trend(p)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        etag = '"' + hashlib.sha256(json.dumps(body["data"], sort_keys=True).encode()).hexdigest()[:20] + '"'
        # private：响应按租户（X-API-Key）区分，public 会让 CDN / 共享代理把一个租户的数据返回给另一个租户。
        # 要上 CDN 就得加 Vary: X-API-Key 或把租户放进 URL，见 design.md §7
        headers = {"ETag": etag, "Cache-Control": f"private, max-age={3600 if ttl >= 3600 else 30}",
                   "X-Cache": body["meta"]["cache"],
                   "X-Query-Plan": ",".join(f'{x["segment"]}:{x["from"]}..{x["to"]}' for x in body["meta"]["plan"])}
        if body["meta"]["stale"]:
            # 不用 Warning: 110：RFC 9111 已废弃 Warning 头。权威标记是 meta.stale，这个头只方便网关 / 日志过滤
            headers["X-Data-Stale"] = "true"
            headers["Cache-Control"] = "no-cache"
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers=headers), body["meta"]
        return Response(payload, media_type="application/json", headers=headers), body["meta"]

    return app


async def _safe(fn) -> bool:
    import asyncio

    try:
        return bool(await asyncio.to_thread(fn))
    except Exception:  # noqa: BLE001
        return False

