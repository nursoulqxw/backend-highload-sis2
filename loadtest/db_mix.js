// k6, SIS #3: a DATABASE-heavy mix through the real API (before / after the DB tuning).
//
//   likes : LikePost                 -> INSERT INTO likes (Postgres write)
//   reads : GetCounters, 20 posts    -> posts from the long tail of 50M are not in Redis,
//                                       so each read = cold-key SELECT on post_counters
//                                       + liked_by_me SELECT on likes
//
// Env: PROTOCOL (grpc | rest, default grpc), LIKES (per s, default 1000), READS (per s, default 1500),
//      DURATION (default 60s), USER_BASE, OUT.

import grpc from 'k6/net/grpc';
import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const PROTOCOL = __ENV.PROTOCOL || 'grpc';
const LIKES = parseInt(__ENV.LIKES || '1000', 10);
const READS = parseInt(__ENV.READS || '1500', 10);
const DURATION = __ENV.DURATION || '60s';
const USER_BASE = parseInt(__ENV.USER_BASE || '1000000000', 10);
const GRPC_TARGET = __ENV.GRPC_TARGET || 'grpc-api:50051';
const REST_URL = __ENV.REST_URL || 'http://rest-api:8000';
const POSTS = 50000000;
const SEEDED_USERS = 10000000; // users 1..10M have likes (db/seed.sql)

const failed = new Counter('failed_calls');
const LATENCY = PROTOCOL === 'grpc' ? 'grpc_req_duration' : 'http_req_duration';

function arrival(rate, fn) {
  return {
    executor: 'constant-arrival-rate', exec: fn, rate, timeUnit: '1s', duration: DURATION,
    preAllocatedVUs: Math.max(20, Math.ceil(rate / 50)), maxVUs: Math.max(200, Math.ceil(rate / 5)),
  };
}

export const options = {
  scenarios: { likes: arrival(LIKES, 'likes'), reads: arrival(READS, 'reads') },
  thresholds: { [`${LATENCY}{scenario:likes}`]: ['max>=0'], [`${LATENCY}{scenario:reads}`]: ['max>=0'] },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

const client = new grpc.Client();
if (PROTOCOL === 'grpc') client.load(['/proto'], 'counter.proto');
let connected = false;

function grpcOk(method, body) {
  if (!connected) {
    client.connect(GRPC_TARGET, { plaintext: true, timeout: '5s' });
    connected = true;
  }
  const res = client.invoke(`counter.v1.CounterService/${method}`, body, { timeout: '500ms' });
  return res && res.status === grpc.StatusOK;
}

function hash(i) {
  return (i * 2654435761) % 4294967296;
}

export function likes() {
  const i = exec.scenario.iterationInTest;
  const user = USER_BASE + i;
  const post = (hash(i) % POSTS) + 1;
  const ok = PROTOCOL === 'grpc'
    ? grpcOk('LikePost', { user_id: user, post_id: post })
    : http.put(`${REST_URL}/api/v1/posts/${post}/like`, null, {
      headers: { 'X-User-Id': String(user) }, timeout: '500ms', tags: { name: 'like' },
    }).status === 204;
  if (!check(ok, { 'like ok': (x) => x })) failed.add(1);
}

export function reads() {
  const i = exec.scenario.iterationInTest;
  const user = (hash(i + 99) % SEEDED_USERS) + 1; // a real user with likes
  const ids = [];
  for (let k = 0; k < 20; k++) ids.push((hash(i * 20 + k + 12345) % POSTS) + 1);
  const ok = PROTOCOL === 'grpc'
    ? grpcOk('GetCounters', { user_id: user, post_ids: ids })
    : http.get(`${REST_URL}/api/v1/counters?post_ids=${ids.join(',')}`, {
      headers: { 'X-User-Id': String(user) }, timeout: '500ms', tags: { name: 'counters' },
    }).status === 200;
  if (!check(ok, { 'read ok': (x) => x })) failed.add(1);
}

function stats(metric) {
  if (!metric) return null;
  const v = metric.values;
  return { p50: v['p(50)'], p95: v['p(95)'], p99: v['p(99)'], max: v.max, avg: v.avg };
}

export function handleSummary(data) {
  const m = data.metrics;
  const summary = {
    protocol: PROTOCOL,
    likes_per_s: LIKES,
    reads_per_s: READS,
    duration: DURATION,
    calls: m.iterations ? m.iterations.values.count : 0,
    failed_calls: m.failed_calls ? m.failed_calls.values.count : 0,
    dropped_calls: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
    like_ms: stats(m[`${LATENCY}{scenario:likes}`]),
    read_ms: stats(m[`${LATENCY}{scenario:reads}`]),
  };
  return {
    [__ENV.OUT || '/results/db_mix.json']: JSON.stringify(summary, null, 2),
    stdout: JSON.stringify(summary) + '\n',
  };
}
