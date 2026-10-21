"""趋势接口的响应结构，只用于生成 OpenAPI 文档。响应体本身由 service.assemble 拼出，不经 Pydantic 校验。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Series(BaseModel):
    platform: str
    values: list[int | None] = Field(description="与 dates 一一对应。0 = 没卖出去；null = 晚于该平台的数据截止")


class TrendData(BaseModel):
    dates: list[str] = Field(description="桶的起始本地日期；首个周/月桶从 start_date 起算")
    series: list[Series]
    total: list[int | None] = Field(
        description="选中平台之和。全部平台为 null 时为 null；部分为 null 时 = 非 null 平台之和，并记入 partial_dates")
    days_in_bucket: list[int] | None = Field(
        None, description="仅 week / month：桶本身的天数（周 7，月 28–31），不受查询区间裁剪")
    days_covered: list[int] | None = Field(
        None, description="仅 week / month：桶内落在查询区间、且不晚于数据截止的天数（截止当天算一整天）")
    partial: list[bool] | None = Field(
        None, description="仅 week / month：days_covered < days_in_bucket，或桶内含 partial_dates 中的日")
    daily_avg: list[float | None] | None = Field(
        None, description="仅 week / month：total / days_covered，保留一位小数；days_covered 为 0 时为 null。"
                          "只给 total 的日均，不给按平台的日均")


class SourceTzWarning(BaseModel):
    code: str = Field(description="SOURCE_TZ_UNCONFIRMED")
    message: str
    platforms: list[str] = Field(description="源时区未确认、且与请求 tz 不同的平台")
    source_tz: dict[str, str] = Field(description="这些平台当前假设的源时区")


class TrendMeta(BaseModel):
    class_slug: str
    rule_version: str
    tz: str
    granularity: str
    start_date: str
    end_date: str
    platforms: list[str]
    data_through: str | None = Field(description="全部平台中最新的事件时间（UTC）")
    data_through_by_platform: dict[str, str | None] = Field(
        description="选中平台各自的截止时间（UTC），从该平台最新事件推断；生产应换成采集侧心跳与接收水位")
    partial_dates: list[str] = Field(description="不完整的本地日：含某平台截止时刻，或部分平台已为 null")
    source_tz_assumed: bool = Field(description="选中平台里有源时区未与采集方确认的平台")
    warnings: list[SourceTzWarning] = Field(description="不影响结果返回的提示；没有时为空数组")
    sum: dict[str, int] = Field(description="区间合计，按平台与 all；null 按 0 计")
    plan: list[dict] = Field(description="查询规划：每段读的表与日期范围")
    cache: str = Field(description="hit | miss | stale")
    stale: bool
    generated_at: str


class TrendResponse(BaseModel):
    data: TrendData
    meta: TrendMeta
