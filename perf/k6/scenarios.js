// k6 固定到达率压测（constant-arrival-rate）：从「计划发送时刻」计时，避免闭环压测的 coordinated omission 低估 P99。
// 用法：k6 run -e SCEN=hot|cold|herd -e API=http://localhost:8000 perf/k6/scenarios.js
import http from 'k6/http';
import { check } from 'k6';

const API = __ENV.API || 'http://localhost:8000';
const SCEN = __ENV.SCEN || 'hot';
const T = '/v1/product-classes/maxi-dress/sales-trend';
const HERD_KEY = __ENV.HERD_KEY || '2024-01-09';

const PLATFORMS = ['', '&platforms=app', '&platforms=website', '&platforms=app,marketplace', '&platforms=marketplace,website'];
const PRESET_TZ = ['UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/London'];
const HEAVY_TZ = ['Asia/Tokyo', 'Australia/Sydney', 'Asia/Kathmandu'];

// 20 个热点 key：近 7 / 30 / 90 天这类看板预设区间 × 预设时区
const HOT = [];
for (const [s, e] of [['2024-03-07', '2024-03-13'], ['2024-02-13', '2024-03-13'], ['2024-01-01', '2024-03-31'],
                      ['2024-02-01', '2024-02-29'], ['2024-01-01', '2024-01-31']]) {
  for (const tz of PRESET_TZ) HOT.push(`${T}?start_date=${s}&end_date=${e}&tz=${tz}`);
}

const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];
const day = (n) => new Date(Date.UTC(2024, 0, 1 + n)).toISOString().slice(0, 10);

function mixedUrl() {
  const r = Math.random();
  if (r < 0.70) return pick(HOT);                                   // 70% 热点
  const a = Math.floor(Math.random() * 70), len = 1 + Math.floor(Math.random() * 30);
  if (r < 0.95) return `${T}?start_date=${day(a)}&end_date=${day(a + len)}&tz=${pick(PRESET_TZ)}${pick(PLATFORMS)}`; // 25% 自定义
  return `${T}?start_date=${day(a)}&end_date=${day(a + len)}&tz=${pick(HEAVY_TZ)}${pick(PLATFORMS)}`;              // 5% 重查询
}

const SCENARIOS = {
  hot: {
    scenarios: { hot: { executor: 'constant-arrival-rate', rate: 1000, timeUnit: '1s', duration: '60s',
                        preAllocatedVUs: 200, maxVUs: 1000 } },
    thresholds: { http_req_duration: ['p(99)<50'], http_req_failed: ['rate<0.001'] },
  },
  cold: {
    scenarios: { cold: { executor: 'constant-arrival-rate', rate: 200, timeUnit: '1s', duration: '60s',
                         preAllocatedVUs: 100, maxVUs: 500 } },
    thresholds: { http_req_duration: ['p(99)<300'], http_req_failed: ['rate<0.001'] },
  },
  herd: {
    // 同一个冷 key 瞬间 500 并发：验证 singleflight，存储层查询次数由 run.sh 从 system.query_log 取
    scenarios: { herd: { executor: 'per-vu-iterations', vus: 500, iterations: 1, maxDuration: '30s' } },
    thresholds: { http_req_failed: ['rate<0.001'] },
  },
};

export const options = { ...SCENARIOS[SCEN], summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'] };

export default function () {
  const url = SCEN === 'hot' ? pick(HOT)
    : SCEN === 'cold' ? mixedUrl()
    : `${T}?start_date=${HERD_KEY}&end_date=2024-03-01&tz=Asia/Shanghai`;
  const res = http.get(API + url, { headers: { 'X-API-Key': `k6-${__VU % 50}` } });
  check(res, { 'status 200': (r) => r.status === 200 });
}
