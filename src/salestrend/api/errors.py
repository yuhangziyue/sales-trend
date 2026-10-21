"""RFC 9457 错误体（application/problem+json），附 code 与 invalid_params。11 个错误码。"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

TITLES = {
    "INVALID_DATE": (400, "Invalid date"),
    "RANGE_INVERTED": (400, "Date range inverted"),
    "RANGE_TOO_LARGE": (400, "Date range too large"),
    "UNKNOWN_PLATFORM": (400, "Unknown platform"),
    "INVALID_TZ": (400, "Invalid time zone"),
    "INVALID_GRANULARITY": (400, "Invalid granularity"),
    "CLASS_NOT_FOUND": (404, "Product class not found"),
    "RULE_VERSION_NOT_FOUND": (404, "Rule version not found"),
    "RATE_LIMITED": (429, "Too many requests"),
    "UPSTREAM_UNAVAILABLE": (503, "Storage unavailable"),
    "QUERY_TIMEOUT": (504, "Query timeout"),
}


class Problem(Exception):
    def __init__(self, code: str, detail: str, invalid_params: list[dict] | None = None,
                 headers: dict[str, str] | None = None):
        self.code, self.detail = code, detail
        self.status, self.title = TITLES[code]
        self.invalid_params = invalid_params or []
        self.headers = headers or {}


def param_error(code: str, name: str, reason: str) -> Problem:
    return Problem(code, f"{name}: {reason}", [{"name": name, "reason": reason}])


async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
    body = {
        "type": f"https://salestrend.example.com/problems/{exc.code.lower().replace('_', '-')}",
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
        "code": exc.code,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    if exc.invalid_params:
        body["invalid_params"] = exc.invalid_params
    return JSONResponse(body, status_code=exc.status, headers=exc.headers, media_type="application/problem+json")
