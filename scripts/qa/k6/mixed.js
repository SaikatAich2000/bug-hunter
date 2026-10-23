// Mixed-traffic load test: 70% reads, 25% writes, 5% heavy downloads, one logged-in admin.
//   docker run --rm -i -v "$PWD/scripts/qa/k6:/s" grafana/k6 run -e STAGE=load -e BASE_URL=http://host.docker.internal:8765 \
//     -e EMAIL=<admin email> -e PASSWORD=<admin password> /s/mixed.js
// STAGE: smoke (1 VU, 1 min) | load (ramp to 50, 10 min) | stress (ramp to 400) | spike (10 to 300 in 10 s) | soak (20 VUs, 30 min)
// Start the app with a raised RATE_LIMIT_SCALE and API_RATE_MAX=0 (or a high value), or the per-IP limits will answer 429.
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const STAGES = {
  smoke: [{ duration: "1m", target: 1 }],
  load: [{ duration: "2m", target: 50 }, { duration: "6m", target: 50 }, { duration: "2m", target: 0 }],
  stress: [{ duration: "2m", target: 100 }, { duration: "2m", target: 200 }, { duration: "2m", target: 300 }, { duration: "2m", target: 400 }, { duration: "1m", target: 0 }],
  spike: [{ duration: "10s", target: 10 }, { duration: "10s", target: 300 }, { duration: "1m", target: 300 }, { duration: "20s", target: 0 }],
  soak: [{ duration: "1m", target: 20 }, { duration: "28m", target: 20 }, { duration: "1m", target: 0 }],
};

export const options = {
  stages: STAGES[__ENV.STAGE || "smoke"],
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{kind:read}": ["p(95)<500"],
    "http_req_duration{kind:write}": ["p(95)<1000"],
    "http_req_duration{kind:heavy}": ["p(95)<3000"],
  },
};

export function setup() {
  const login = http.post(`${BASE}/api/auth/login`, JSON.stringify({ email: __ENV.EMAIL, password: __ENV.PASSWORD }), { headers: { "Content-Type": "application/json" } });
  check(login, { "login ok": (r) => r.status === 200 });
  const jar = http.cookieJar();
  const cookies = jar.cookiesForURL(BASE);
  const name = Object.keys(cookies).find((k) => k.endsWith("bh_session"));
  const projects = http.get(`${BASE}/api/projects`, { headers: { Cookie: `${name}=${cookies[name][0]}` } }).json();
  return { cookie: `${name}=${cookies[name][0]}`, project: projects[0].id };
}

export default function (d) {
  const h = { headers: { Cookie: d.cookie, "Content-Type": "application/json" } };
  const roll = Math.random();
  if (roll < 0.7) {
    const path = ["/api/bugs?page_size=20", "/api/projects", "/api/stats", "/api/bugs?status=New&page_size=20", "/api/auth/me"][Math.floor(Math.random() * 5)];
    check(http.get(`${BASE}${path}`, { ...h, tags: { kind: "read" } }), { "read 200": (r) => r.status === 200 });
  } else if (roll < 0.95) {
    const created = http.post(`${BASE}/api/bugs`, JSON.stringify({ project_id: d.project, title: `k6 ${__VU}-${__ITER}` }), { ...h, tags: { kind: "write" } });
    check(created, { "create 201": (r) => r.status === 201 });
    // Comments are throttled per user (COMMENT_RATE_MAX), and this test is one user: post them at a human pace.
    if (created.status === 201 && Math.random() < 0.05) {
      http.post(`${BASE}/api/bugs/${created.json().id}/comments`, JSON.stringify({ body: "k6 comment" }), { ...h, tags: { kind: "write" } });
    }
  } else {
    check(http.get(`${BASE}/api/bugs/import/template.xlsx`, { ...h, tags: { kind: "heavy" } }), { "download 200": (r) => r.status === 200 });
  }
  sleep(0.5 + Math.random());
}
