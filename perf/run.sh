#!/usr/bin/env bash
# make perf：P1 热缓存 / P2 冷混合 / P3 击穿 / P6 任意区间命中率 / P4 降级，结果写 perf/results/，汇总成 docs/perf-report.md
# 只跑一部分：SCENS="range" bash perf/run.sh（make perf-p6）。P5 千万级是单独的 make perf-p5
set -uo pipefail
SCENS=${SCENS:-"hot cold herd range degrade"}
want() { [[ " $SCENS " == *" $1 "* ]]; }
API=${1:-http://localhost:8000}
DIR=$(cd "$(dirname "$0")/.." && pwd)
OUT=$DIR/perf/results
mkdir -p "$OUT"
DC="docker compose -f $DIR/docker-compose.yml"
ch() { $DC exec -T clickhouse clickhouse-client -u "${CH_USER:-salestrend}" --password "${CH_PASSWORD:-salestrend}" -q "$1"; }

# 压测期间放开限流：要测的是存储与缓存，不是令牌桶
export RATE_LIMIT_PER_S=100000 RATE_LIMIT_BURST=100000
$DC up -d --wait api >/dev/null

run() {  # $1 场景名，$2 结果文件名（默认同场景名）
  local name=${2:-$1}
  k6 run --quiet -e SCEN="$1" -e API="$API" -e HERD_KEY="${HERD_KEY:-}" \
    --summary-export "$OUT/$name.json" "$DIR/perf/k6/scenarios.js" > "$OUT/$name.log" 2>&1
  echo "$name exit=$?"
}

if want hot; then
echo "== P1 hot：先预热 20 个热点 key"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
k6 run --quiet -e SCEN=hot -e API="$API" --iterations 400 --vus 20 "$DIR/perf/k6/scenarios.js" >/dev/null 2>&1
run hot
fi

if want cold; then
echo "== P2 cold：清空缓存后按 70/25/5 混合"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
run cold
fi

if want herd; then
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
fi

if want range; then
# 命中率与回源率从 /metrics 前后快照的差值算（多 worker 已由多进程模式汇总），k6 按 X-Cache 头计数交叉验证。
# 两层都清：整响应（trend:* 与 stale:trend:*）和月块（blk:*）。不 FLUSHALL：data_gen 也在 Redis 里，
# 清掉会让 key 前缀变化，但对「从空缓存开始」没有区别。
# 冷启动曲线：压测期间每 10 秒抓一次 /metrics，report.py 按相邻两次快照的差值算每个窗口的命中率与回源率。
# P6_SUFFIX 给结果文件名加后缀：BLOCK_CACHE=0 起 API 再跑一轮、P6_SUFFIX=_noblock，就是同一台机器上的只有一层的对照
for s in ${P6_SCENS:-range hot200}; do
  name="${s}${P6_SUFFIX:-}"
  echo "== P6 ${name}：清空两层缓存后 200 RPS × 120s"
  $DC exec -T redis sh -c "redis-cli --scan --pattern '*trend:*' | xargs -r redis-cli DEL; \
                           redis-cli --scan --pattern 'blk:*' | xargs -r redis-cli DEL" >/dev/null
  rm -f "$OUT/${name}"_metrics_t*.txt
  curl -s "$API/metrics" > "$OUT/${name}_metrics_before.txt"
  ( i=0; while sleep 10; do i=$((i + 10)); curl -s "$API/metrics" > "$OUT/${name}_metrics_t$(printf %03d $i).txt"
      [ $i -ge 120 ] && break; done ) &
  sampler=$!
  run "$s" "$name"
  wait $sampler 2>/dev/null
  curl -s "$API/metrics" > "$OUT/${name}_metrics_after.txt"
done
fi

if want degrade; then
echo "== P4 降级：停掉 ClickHouse"
U="$API/v1/product-classes/maxi-dress/sales-trend"
code() { curl -s -D "$OUT/p4_headers.txt" -o "$OUT/p4_body.json" -w "%{http_code}" "$1"; }
curl -s -o /dev/null "$U?start_date=2024-01-01&end_date=2024-01-31"           # 先让这个 key 进缓存
# 新鲜副本过期，只剩 stale 副本；月块也清掉，否则这个 key 会由月块拼出、根本不查库（那是另一条降级路径，单元测试覆盖）
$DC exec -T redis sh -c "redis-cli --scan --pattern 'trend:*' | xargs -r redis-cli DEL; \
                         redis-cli --scan --pattern 'blk:*' | xargs -r redis-cli DEL" >/dev/null
$DC stop clickhouse >/dev/null
{
  echo "cached_key_status=$(code "$U?start_date=2024-01-01&end_date=2024-01-31")"
  echo "cached_key_meta=$(python3 -c 'import json;m=json.load(open("'"$OUT"'/p4_body.json"))["meta"];print(m["cache"], "stale=%s" % m["stale"])')"
  echo "cached_key_stale_header=$(grep -i '^x-data-stale' "$OUT/p4_headers.txt" | tr -d '\r')"
  echo "uncached_key_status=$(code "$U?start_date=2024-02-02&end_date=2024-02-09")"
  echo "uncached_key_code=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/p4_body.json")).get("code"))')"
} > "$OUT/degrade.txt"
$DC start clickhouse >/dev/null && $DC up -d --wait clickhouse >/dev/null
cat "$OUT/degrade.txt"
fi

uv run --project "$DIR" python "$DIR/perf/report.py" "$OUT" "$DIR/docs/perf-report.md"
