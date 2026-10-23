// 闭环并发：VUS 个虚拟用户「发请求 → 等返回 → 立刻再发」，即任一时刻约 VUS 个请求在途。样例库上对照两条缓存路径：
// 用法：k6 run -e MODE=hit|block -e VUS=100 -e DURATION=30s -e API=http://localhost:8000 perf/k6/vus.js
// （hit 先用 -e VUS=20 -e DURATION=3s 跑一遍预热；结果与 docker stats 采样见 docs/perf-report.md P6）
//   MODE=hit    20 个热点 key（P1 的 key 集），预热后全部第一层命中
//   MODE=block  任意区间，但终点不晚于 03-12（全部已封口）：第一层基本不中、月块全部命中，不查库
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

export const options = {
  scenarios: { c: { executor: 'constant-vus', vus: Number(__ENV.VUS || 100), duration: __ENV.DURATION || '30s' } },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};
const T = '/v1/product-classes/maxi-dress/sales-trend';
const API = __ENV.API || 'http://localhost:8000';
const MODE = __ENV.MODE || 'block';
const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];
const day = (n) => new Date(Date.UTC(2024, 0, 1 + n)).toISOString().slice(0, 10);
const PLATFORM_SETS = ['app', 'marketplace', 'website', 'app,marketplace', 'app,website', 'marketplace,website',
                       'app,marketplace,website'];
const TZ = ['UTC', 'Asia/Shanghai', 'America/Los_Angeles', 'America/New_York', 'Europe/London', 'Asia/Kolkata'];
const HOT = [];
for (const [s, e] of [['2024-03-07', '2024-03-13'], ['2024-02-13', '2024-03-13'], ['2024-01-01', '2024-03-31'],
                      ['2024-02-01', '2024-02-29'], ['2024-01-01', '2024-01-31']])
  for (const tz of ['UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/London'])
    HOT.push(`${T}?start_date=${s}&end_date=${e}&tz=${tz}`);
const SEALED_DAYS = 72;  // 01-01..03-12
function blockUrl() {
  const len = 1 + Math.floor(Math.random() * SEALED_DAYS);
  const a = Math.floor(Math.random() * (SEALED_DAYS - len + 1));
  const r = Math.random();
  const g = r < 0.8 ? 'day' : r < 0.9 ? 'week' : 'month';
  return `${T}?start_date=${day(a)}&end_date=${day(a + len - 1)}&tz=${pick(TZ)}&platforms=${pick(PLATFORM_SETS)}` +
         `&granularity=${g}`;
}
const KINDS = ['hit', 'block', 'block_partial', 'miss', 'stale'];
const XC = Object.fromEntries(KINDS.map((k) => [k, new Counter(`xcache_${k}`)]));
const XL = Object.fromEntries(KINDS.map((k) => [k, new Trend(`xcache_${k}_ms`, true)]));
export default function () {
  const res = http.get(API + (MODE === 'hit' ? pick(HOT) : blockUrl()), { headers: { 'X-API-Key': `vu-${__VU}` } });
  check(res, { '200': (r) => r.status === 200 });
  const xc = res.headers['X-Cache'];
  if (XC[xc]) { XC[xc].add(1); XL[xc].add(res.timings.duration); }
}
