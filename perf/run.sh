#!/usr/bin/env bash
# make perf：P1 热缓存 / P2 冷混合 / P3 击穿，结果写 perf/results/，汇总成 docs/perf-report.md
set -uo pipefail
API=${1:-http://localhost:8000}
DIR=$(cd "$(dirname "$0")/.." && pwd)
OUT=$DIR/perf/results
mkdir -p "$OUT"
DC="docker compose -f $DIR/docker-compose.yml"
ch() { $DC exec -T clickhouse clickhouse-client -u "${CH_USER:-salestrend}" --password "${CH_PASSWORD:-salestrend}" -q "$1"; }

# 压测期间放开限流：要测的是存储与缓存，不是令牌桶
export RATE_LIMIT_PER_S=100000 RATE_LIMIT_BURST=100000
$DC up -d --wait api >/dev/null

run() {  # $1 场景名
  k6 run --quiet -e SCEN="$1" -e API="$API" -e HERD_KEY="${HERD_KEY:-}" \
    --summary-export "$OUT/$1.json" "$DIR/perf/k6/scenarios.js" > "$OUT/$1.log" 2>&1
  echo "$1 exit=$?"
}

echo "== P1 hot：先预热 20 个热点 key"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
k6 run --quiet -e SCEN=hot -e API="$API" --iterations 400 --vus 20 "$DIR/perf/k6/scenarios.js" >/dev/null 2>&1
run hot

echo "== P2 cold：清空缓存后按 70/25/5 混合"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
run cold

echo "== P3 herd：同一冷 key 500 并发"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
ch "SYSTEM FLUSH LOGS"
T0=$(ch "SELECT now64(3)")
export HERD_KEY=2024-01-0$((RANDOM % 9 + 1))
run herd
sleep 2
ch "SYSTEM FLUSH LOGS"
ch "SELECT count() FROM system.query_log WHERE type = 'QueryFinish' AND event_time_microseconds >= '$T0'
    AND query LIKE '%agg_class_daily%' AND query NOT LIKE '%system.query_log%'" > "$OUT/herd_storage_queries.txt"
echo "herd storage queries: $(cat "$OUT/herd_storage_queries.txt")"

echo "== P4 降级：停掉 ClickHouse"
U="$API/v1/product-classes/maxi-dress/sales-trend"
code() { curl -s -o "$OUT/p4_body.json" -w "%{http_code}" "$1"; }
curl -s -o /dev/null "$U?start_date=2024-01-01&end_date=2024-01-31"           # 先让这个 key 进缓存
$DC exec -T redis sh -c "redis-cli --scan --pattern 'trend:*' | xargs -r redis-cli DEL" >/dev/null  # 新鲜副本过期，只剩 stale 副本
$DC stop clickhouse >/dev/null
{
  echo "cached_key_status=$(code "$U?start_date=2024-01-01&end_date=2024-01-31")"
  echo "cached_key_meta=$(python3 -c 'import json;m=json.load(open("'"$OUT"'/p4_body.json"))["meta"];print(m["cache"], "stale=%s" % m["stale"])')"
  echo "uncached_key_status=$(code "$U?start_date=2024-02-02&end_date=2024-02-09")"
  echo "uncached_key_code=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/p4_body.json")).get("code"))')"
} > "$OUT/degrade.txt"
$DC start clickhouse >/dev/null && $DC up -d --wait clickhouse >/dev/null
cat "$OUT/degrade.txt"

uv run --project "$DIR" python "$DIR/perf/report.py" "$OUT" "$DIR/docs/perf-report.md"
