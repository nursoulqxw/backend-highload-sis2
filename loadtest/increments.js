// k6 load test: the increment workload of T7 (views + likes) at a FIXED rate.
//
// Why a fixed rate (open model): users do not wait for each other. If the server gets
// slow, new requests still arrive. A "50 at a time" test (closed model) slows down
// together with the server and hides the real tail latency.
//
// Env vars:
//   PROTOCOL   grpc | rest                                   (default grpc)
//   MODE       unary = one view per call                     (default batch)
//              batch = views in batches of BATCH (as the API Gateway sends them)
//   RATE       INCREMENTS per second, views + likes          (default 20000)
//   BATCH      views per batch call                          (default 100)
//   LIKE_PCT   % of increments that are likes (always unary) (default 5)
//   DURATION   e.g. 60s                                      (default 60s)
//   USER_BASE  first user id; change it per run so every like / view is new (default 1e9)
//   PROBE      1 = also measure the consistency lag          (default 1)
//   OUT        file for the JSON summary                     (default /results/summary.json)
//
// Each like / view uses a NEW (user, post) pair, so it does the real work, not the
// cheap "already liked / already seen" path.

import grpc from 'k6/net/grpc';
import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const PROTOCOL = __ENV.PROTOCOL || 'grpc';
const MODE = __ENV.MODE || 'batch';
const RATE = parseInt(__ENV.RATE || '20000', 10);
const BATCH = MODE === 'batch' ? parseInt(__ENV.BATCH || '100', 10) : 1;
const LIKE_PCT = parseFloat(__ENV.LIKE_PCT || '5');
const DURATION = __ENV.DURATION || '60s';
const USER_BASE = parseInt(__ENV.USER_BASE || '1000000000', 10);
const PROBE = (__ENV.PROBE || '1') === '1';
const GRPC_TARGET = __ENV.GRPC_TARGET || 'grpc-api:50051';
const REST_URL = __ENV.REST_URL || 'http://rest-api:8000';

const POSTS = 50000000; // 50M posts
// Real traffic is skewed: most views go to new / popular posts. 90% of increments go to
// 100K "hot" posts, 10% to any of the 50M (the long tail: often a cold key -> Postgres read).
const HOT_POSTS = 100000;
const HOT_PCT = 90;

const likeRate = Math.round((RATE * LIKE_PCT) / 100);
const viewCallRate = Math.max(1, Math.round((RATE - likeRate) / BATCH));

const increments = new Counter('increments'); // views + likes that the server accepted
const failed = new Counter('failed_calls');
const consistencyLag = new Trend('consistency_lag_ms', true);

function vus(rate) {
  // Enough VUs ready from the first second: otherwise k6 itself drops requests at the start
  // (a VU's first call also opens its connection) and that looks like a server problem.
  return { preAllocatedVUs: Math.max(50, Math.ceil(rate / 20)), maxVUs: Math.max(200, Math.ceil(rate / 10)) };
}

const scenarios = {
  views: { executor: 'constant-arrival-rate', exec: 'views', rate: viewCallRate, timeUnit: '1s', duration: DURATION, ...vus(viewCallRate) },
};
if (likeRate > 0) {
  scenarios.likes = { executor: 'constant-arrival-rate', exec: 'likes', rate: likeRate, timeUnit: '1s', duration: DURATION, ...vus(likeRate) };
}
if (PROBE) {
  scenarios.probe = { executor: 'constant-arrival-rate', exec: 'probe', rate: 10, timeUnit: '1s', duration: DURATION, preAllocatedVUs: 5, maxVUs: 50 };
}

// Built-in latency metric of the protocol, split by scenario (a threshold makes k6 keep the split).
const LATENCY = PROTOCOL === 'grpc' ? 'grpc_req_duration' : 'http_req_duration';
const thresholds = {};
for (const name of Object.keys(scenarios)) thresholds[`${LATENCY}{scenario:${name}}`] = ['max>=0'];

export const options = {
  discardResponseBodies: false,
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

// ---------- ids ----------

function hash(i) {
  return (i * 2654435761) % 4294967296; // a cheap hash, spreads the ids
}
function postFor(i) {
  const h = hash(i);
  return (h % 100) < HOT_PCT ? (h % HOT_POSTS) + 1 : (h % POSTS) + 1;
}
// Different user id ranges per scenario, so no two calls share a (user, post) pair.
const VIEW_USERS = USER_BASE;
const LIKE_USERS = USER_BASE + 500000000;
const PROBE_USERS = USER_BASE + 900000000;

// ---------- protocol calls ----------

const client = new grpc.Client();
if (PROTOCOL === 'grpc') client.load(['/proto'], 'counter.proto');
let connected = false;

function grpcCall(method, body) {
  if (!connected) {
    client.connect(GRPC_TARGET, { plaintext: true, timeout: '5s' });
    connected = true;
  }
  const res = client.invoke(`counter.v1.CounterService/${method}`, body, { timeout: '500ms' });
  return res && res.status === grpc.StatusOK ? res.message || {} : null;
}

function restCall(method, path, body, userId) {
  const params = { timeout: '500ms', headers: {}, tags: { name: path.split('/').slice(0, 4).join('/') } };
  if (userId) params.headers['X-User-Id'] = String(userId);
  if (body) params.headers['Content-Type'] = 'application/json';
  const res = http.request(method, `${REST_URL}${path}`, body ? JSON.stringify(body) : null, params);
  if (res.status !== 200 && res.status !== 204) return null;
  return res.status === 200 ? res.json() : {};
}

function sendViews(views) {
  // views = [{user_id, post_id}, ...]
  if (MODE === 'unary') {
    const v = views[0];
    return PROTOCOL === 'grpc'
      ? grpcCall('RecordView', v)
      : restCall('POST', `/api/v1/posts/${v.post_id}/view`, null, v.user_id);
  }
  return PROTOCOL === 'grpc'
    ? grpcCall('RecordViews', { views })
    : restCall('POST', '/api/v1/views:batch', { views }, null);
}

function readViews(postId) {
  const res = PROTOCOL === 'grpc'
    ? grpcCall('GetCounters', { user_id: 0, post_ids: [postId] })
    : restCall('GET', `/api/v1/counters?post_ids=${postId}`, null, null);
  if (!res) return -1;
  const rows = PROTOCOL === 'grpc' ? res.counters || [] : res;
  return rows.length ? parseInt(rows[0].views, 10) : -1;
}

// ---------- scenarios ----------

export function views() {
  const call = exec.scenario.iterationInTest;
  const batch = [];
  for (let k = 0; k < BATCH; k++) {
    const i = call * BATCH + k;
    batch.push({ user_id: VIEW_USERS + i, post_id: postFor(i) });
  }
  const ok = sendViews(batch) !== null;
  if (check(ok, { 'views ok': (x) => x })) increments.add(BATCH);
  else failed.add(1);
}

export function likes() {
  const i = exec.scenario.iterationInTest;
  const user = LIKE_USERS + i;
  const post = postFor(i + 7777777);
  const ok = (PROTOCOL === 'grpc'
    ? grpcCall('LikePost', { user_id: user, post_id: post })
    : restCall('PUT', `/api/v1/posts/${post}/like`, null, user)) !== null;
  if (check(ok, { 'like ok': (x) => x })) increments.add(1);
  else failed.add(1);
}

// Eventual consistency: send a view of a brand-new post, then read until we see it.
// The lag = time from sending the view until a read returns it. Requirement: <= 2 s.
export function probe() {
  const i = exec.scenario.iterationInTest;
  const post = 60000000 + (USER_BASE % 1000000) * 100 + i; // outside the 50M load ids, new every time
  const started = Date.now();
  sendViews([{ user_id: PROBE_USERS + i, post_id: post }]);
  while (Date.now() - started < 5000) {
    if (readViews(post) >= 1) {
      consistencyLag.add(Date.now() - started);
      return;
    }
    sleep(0.005);
  }
  consistencyLag.add(5000); // not visible after 5 s = a fail
}

// ---------- summary ----------

function stats(metric) {
  if (!metric) return null;
  const v = metric.values;
  return { p50: v['p(50)'], p95: v['p(95)'], p99: v['p(99)'], max: v.max, avg: v.avg };
}

export function handleSummary(data) {
  const m = data.metrics;
  const seconds = parseInt(DURATION, 10);
  const dropped = m.dropped_iterations ? m.dropped_iterations.values.count : 0;
  const summary = {
    protocol: PROTOCOL,
    mode: MODE,
    batch: BATCH,
    target_increments_per_s: RATE,
    achieved_increments_per_s: m.increments ? m.increments.values.count / seconds : 0,
    calls_per_s: viewCallRate + likeRate,
    duration: DURATION,
    like_pct: LIKE_PCT,
    failed_calls: m.failed_calls ? m.failed_calls.values.count : 0,
    dropped_calls: dropped,
    view_call_ms: stats(m[`${LATENCY}{scenario:views}`]),
    like_call_ms: stats(m[`${LATENCY}{scenario:likes}`]),
    consistency_lag_ms: stats(m.consistency_lag_ms),
    max_vus: m.vus_max ? m.vus_max.values.max : null,
  };
  return {
    [__ENV.OUT || '/results/summary.json']: JSON.stringify(summary, null, 2),
    stdout: JSON.stringify(summary) + '\n',
  };
}
