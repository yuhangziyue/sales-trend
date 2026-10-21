#!/usr/bin/env bash
# make demo：打印 8 条典型请求与真实响应（响应用 python 精简显示，完整响应去掉 | short 即可）
set -euo pipefail
API=${1:-http://localhost:8000}
T=/v1/product-classes/maxi-dress/sales-trend

short() { python3 "$(dirname "$0")/_short.py"; }

run() {
  echo; echo "### $1"; echo "\$ curl '$API$2'"
  curl -s "$API$2" | short
}

run "1. 全平台、UTC、1–3 月（3/14 以后为 null，3/13 为未封口的 partial）" "$T?start_date=2024-01-01&end_date=2024-03-31"
run "2. 多平台筛选（顺序无关，命中同一缓存 key）" "$T?start_date=2024-01-01&end_date=2024-01-07&platforms=website,app"
run "3. 换时区：Asia/Shanghai 下 2 月（跨月探针行换日，合计 415）" "$T?start_date=2024-02-01&end_date=2024-02-29&tz=Asia/Shanghai&granularity=month"
run "4. 非预设整点时区走小时表段" "$T?start_date=2024-02-01&end_date=2024-02-03&tz=Asia/Tokyo"
run "5. 规则 v2 影子版本（Elegant Long Dress 计入），不需要重算事实和小时表" "$T?start_date=2024-01-01&end_date=2024-03-31&granularity=month&rule_version=v2"
run "6. 超出数据范围：全部 null，不是 0" "$T?start_date=2024-04-01&end_date=2024-04-03"
run "7. 错误：日期区间倒置" "$T?start_date=2024-02-01&end_date=2024-01-01"
run "8. 错误：固定偏移不是 IANA 时区" "$T?start_date=2024-02-01&end_date=2024-02-02&tz=%2B08:00"
