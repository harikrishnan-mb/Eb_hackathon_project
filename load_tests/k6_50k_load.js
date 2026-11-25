// k6 load test for POST /api/v1/agent/query (CLAUDE.md §14; Blueprint Gates 15-16).
//
// Full profile (Blueprint stages): 2m -> 10K VUs, 3m -> 50K, 5m at 50K, 2m -> 0.
// Thresholds follow the KPI gates (M6 / C9 / C19): p50 < 240, p95 < 450, p99 < 590 ms.
//
// Environment:
//   BASE_URL  API base URL (default http://localhost:8000). Use a staging host here.
//   SMOKE=1   short profile for a local smoke run (2 VUs for 20 s). Not a KPI run.
//
// One machine cannot generate 50,000 VUs. A full run needs distributed injectors
// (k6 cloud or k6-operator). This file only defines the test.

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const SMOKE = __ENV.SMOKE === '1';

export const options = {
  stages: SMOKE
    ? [{ duration: '20s', target: 2 }]
    : [
        { duration: '2m', target: 10000 },
        { duration: '3m', target: 50000 },
        { duration: '5m', target: 50000 },
        { duration: '2m', target: 0 },
      ],
  thresholds: {
    http_req_duration: ['p(50)<240', 'p(95)<450', 'p(99)<590'],
    http_req_failed: ['rate<0.0005'],
    checks: ['rate>0.999'],
  },
};

// Varied queries (golden-set questions) so cache hits follow a realistic repeat rate, not 100%.
const QUERIES = JSON.parse(open('./queries.json'));

export default function () {
  const tenant = `tenant_${__VU % 50}`;   // tenant_${vuId % 50}, per the Blueprint script
  const user = `user_${__VU}`;
  const query = QUERIES[(__VU + __ITER) % QUERIES.length];
  const payload = JSON.stringify({ tenant_id: tenant, user_id: user, query: query });

  const res = http.post(`${BASE}/api/v1/agent/query`, payload, {
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': tenant,
      'X-User-ID': user,
    },
  });

  let body = {};
  try {
    body = res.json();
  } catch (e) {
    body = {};
  }

  check(res, {
    'status 200': (r) => r.status === 200,
    'latency <= 600ms': (r) => r.timings.duration <= 600,
    // KPI-28 probe: the response tenant must equal the request tenant.
    'no cross-tenant leak': () => body.tenant_id === tenant,
  });

  sleep(1);
}
