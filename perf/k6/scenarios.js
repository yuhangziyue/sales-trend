// k6 固定到达率压测（constant-arrival-rate）：从「计划发送时刻」计时，避免闭环压测的 coordinated omission 低估 P99。
// 用法：k6 run -e SCEN=hot|cold|herd|range|hot200 -e API=http://localhost:8000 perf/k6/scenarios.js
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

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

// P6 任意区间：起止都落在样例数据范围 2024-01-01..03-13（73 天）内。长度先在 1..min(90, 73) 里均匀取，
// 再在放得下的起点里均匀取：先取起点再截断终点，会让大量请求挤到同一个终点上，人为抬高命中率
const DATA_DAYS = 73;
const PLATFORM_SETS = ['app', 'marketplace', 'website', 'app,marketplace', 'app,website', 'marketplace,website',
                       'app,marketplace,website'];   // 7 种非空组合
const ALL_PRESET_TZ = ['UTC', 'Asia/Shanghai', 'America/Los_Angeles', 'America/New_York', 'Europe/London',
                       'Asia/Kolkata'];            // config/platforms.yaml 的全部预设时区

function rangeUrl() {
  const len = 1 + Math.floor(Math.random() * Math.min(90, DATA_DAYS));
  const a = Math.floor(Math.random() * (DATA_DAYS - len + 1));
  const r = Math.random();
  const g = r < 0.8 ? 'day' : r < 0.9 ? 'week' : 'month';   // 以 day 为主
  return `${T}?start_date=${day(a)}&end_date=${day(a + len - 1)}&tz=${pick(ALL_PRESET_TZ)}` +
         `&platforms=${pick(PLATFORM_SETS)}&granularity=${g}`;
}

// P5 端到端，四个场景（-e P5=...），都让并发请求落在不同 key 上：singleflight 合并不了，每个请求都真打存储
//   agg_hourly / agg_kathmandu / agg_daily  一年区间，起止各抖动 0–14 天：Berlin、Kathmandu 走品类 15 分钟表段，
//                           UTC 走日表段（agg_hourly 沿用旧名，便于和优化前对照）
//   rt_utc / rt_berlin / rt_kathmandu  近 30 天：终点固定在 12-31（p5.py 把这天设成未封口，走明细段），
//                           起点抖动 0–6 天 × 7 种平台组合
// 默认规则版本 v1：p5 库的成员表就建在 v1 下
const P5_MODE = __ENV.P5 || 'agg_hourly';
function p5Url() {
  if (P5_MODE.startsWith('rt_')) {
    const tz = { rt_utc: 'UTC', rt_berlin: 'Europe/Berlin', rt_kathmandu: 'Asia/Kathmandu' }[P5_MODE];
    return `${T}?start_date=${day(335 + Math.floor(Math.random() * 7))}&end_date=2024-12-31&tz=${tz}` +
           `&platforms=${pick(PLATFORM_SETS)}`;
  }
  const a = Math.floor(Math.random() * 15), b = Math.floor(Math.random() * 15);
  const tz = { agg_daily: 'UTC', agg_hourly: 'Europe/Berlin', agg_kathmandu: 'Asia/Kathmandu' }[P5_MODE];
  return `${T}?start_date=${day(a)}&end_date=${day(365 - b)}&tz=${tz}`;
}

// 按 X-Cache 响应头分类计数与计时，和 /metrics 的 trend_requests_total 交叉验证。
// block / block_partial 是第二层月块：全部由月块拼出 / 部分来自月块、其余现查
const XC_KINDS = ['hit', 'block', 'block_partial', 'miss', 'stale'];
const XC = Object.fromEntries(XC_KINDS.map((k) => [k, new Counter(`xcache_${k}`)]));
const XC_LAT = Object.fromEntries(XC_KINDS.map((k) => [k, new Trend(`xcache_${k}_ms`, true)]));

const SCENARIOS = {
  hot: {
    scenarios: { hot: { executor: 'constant-arrival-rate', rate: 1000, timeUnit: '1s', duration: '60s',
                        preAllocatedVUs: 200, maxVUs: 1000 } },
    thresholds: { http_req_duration: ['p(99)<50'], http_req_failed: ['rate<0.001'] },
  },
  hit: {
    // 容量：缓存命中路径的拐点。与 hot 同一组 20 个热点 key，到达率由 RATE 给，perf/capacity.py 逐档加压
    scenarios: { hit: { executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 1000), timeUnit: '1s',
                        duration: __ENV.DURATION || '30s', preAllocatedVUs: 200, maxVUs: 3000 } },
    thresholds: { http_req_duration: ['p(99)<20'], http_req_failed: ['rate<0.001'] },
  },
  cold: {
    scenarios: { cold: { executor: 'constant-arrival-rate', rate: 200, timeUnit: '1s', duration: '60s',
                         preAllocatedVUs: 100, maxVUs: 500 } },
    thresholds: { http_req_duration: ['p(99)<300'], http_req_failed: ['rate<0.001'] },
  },
  range: {
    // P6：固定到达率 200 RPS × 120s，缓存预先清空，任意区间。不设延迟阈值：这一场要的是命中率
    scenarios: { range: { executor: 'constant-arrival-rate', rate: 200, timeUnit: '1s', duration: __ENV.DURATION || '120s',
                          preAllocatedVUs: 100, maxVUs: 500 } },
    thresholds: { http_req_failed: ['rate<0.001'] },
  },
  hot200: {
    // P6 对照：同样 200 RPS × 120s、同样从空缓存开始，只打 P1 的 20 个热点 key
    scenarios: { hot200: { executor: 'constant-arrival-rate', rate: 200, timeUnit: '1s', duration: '120s',
                           preAllocatedVUs: 100, maxVUs: 500 } },
    thresholds: { http_req_failed: ['rate<0.001'] },
  },
  p5: {
    // P5 端到端：指向 p5 库的 API 实例，缓存旁路。RATE 可调。阈值即用户给的目标：已聚合 100 ms，实时 150 ms
    scenarios: { p5: { executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 10), timeUnit: '1s',
                       duration: __ENV.DURATION || '60s', preAllocatedVUs: 50, maxVUs: 300 } },
    thresholds: { http_req_duration: [`p(99)<${P5_MODE.startsWith('rt_') ? 150 : 100}`],
                  http_req_failed: ['rate<0.001'] },
  },
  herd: {
    // 同一个冷 key 瞬间 500 并发：验证 singleflight，存储层查询次数由 run.sh 从 system.query_log 取
    scenarios: { herd: { executor: 'per-vu-iterations', vus: 500, iterations: 1, maxDuration: '30s' } },
    thresholds: { http_req_failed: ['rate<0.001'] },
  },
};

export const options = { ...SCENARIOS[SCEN], summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'] };

export default function () {
  const url = SCEN === 'hot' || SCEN === 'hot200' || SCEN === 'hit' ? pick(HOT)
    : SCEN === 'cold' ? mixedUrl()
    : SCEN === 'range' ? rangeUrl()
    : SCEN === 'p5' ? p5Url()
    : `${T}?start_date=${HERD_KEY}&end_date=2024-03-01&tz=Asia/Shanghai`;
  const res = http.get(API + url, { headers: { 'X-API-Key': `k6-${__VU % 50}` } });
  check(res, { 'status 200': (r) => r.status === 200 });
  const xc = res.headers['X-Cache'];
  if (XC[xc]) { XC[xc].add(1); XC_LAT[xc].add(res.timings.duration); }
}
