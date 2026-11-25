// KPI-36 soak (CLAUDE.md §14): steady load for SOAK_MINUTES (default 60) to expose leaks.
// RSS growth is measured outside k6 (Prometheus node exporter or ps), not here.
// Environment: BASE_URL (default http://localhost:8000), SOAK_MINUTES, SOAK_VUS (default 20).
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const MINUTES = parseInt(__ENV.SOAK_MINUTES || '60', 10);
const VUS = parseInt(__ENV.SOAK_VUS || '20', 10);
const QUERIES = JSON.parse(open('./queries.json'));

export const options = {
  scenarios: { soak: { executor: 'constant-vus', vus: VUS, duration: `${MINUTES}m` } },
  thresholds: {
    http_req_failed: ['rate<0.0005'],
    checks: ['rate>0.999'],
  },
};

export default function () {
  const tenant = `tenant_${__VU % 50}`;
  const user = `user_${__VU}`;
  const query = QUERIES[(__VU + __ITER) % QUERIES.length];
  const res = http.post(`${BASE}/api/v1/agent/query`, JSON.stringify({ tenant_id: tenant, user_id: user, query }), {
    headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenant, 'X-User-ID': user },
    timeout: '120s',
  });
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(1);
}
