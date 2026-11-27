#!/bin/sh
# Connection pool sizes: the same load with asyncpg pool = 2 / 5 / 10 / 25 / 50 per process,
# once with direct connections to Postgres and once through PgBouncer.
#
#   sh db/run_pool.sh          # needs the seeded DB on schema 0002; ~15 min
#
# 4 processes per API x 2 APIs = 8 pools. Direct: up to 8 x pool connections to Postgres
# (8 x 25 = 200 > max_connections 100). PgBouncer: at most 40 real connections, whatever the pool.
# Reads go to the primary in all runs, so only the connection path and the pool size change.
#
# Results: benchmark/db/pool_<mode>_<size>.json (k6 summary + max_backends)
set -e
cd "$(dirname "$0")/.."
OUT=benchmark/db
TUNED="docker compose -f docker-compose.yml -f docker-compose.db.yml"
DIRECT=postgresql://postgres:postgres@postgres:5432/counters

backends() {  # real client connections to the counters database right now
  docker compose exec -T postgres psql -U postgres -At -c \
    "SELECT count(*) FROM pg_stat_activity WHERE datname = 'counters' AND backend_type = 'client backend'" \
    2>/dev/null || echo -1
}

for mode in direct pgbouncer; do
  for size in 2 5 10 25 50; do
    if [ "$mode" = direct ]; then
      export APP_DATABASE_URL=$DIRECT DB_BEHIND_PGBOUNCER=false
    else
      unset APP_DATABASE_URL; export DB_BEHIND_PGBOUNCER=true
    fi
    min=2; [ "$size" -lt 2 ] && min=$size
    DATABASE_READ_URL= DB_POOL_MIN=$min DB_POOL_MAX=$size ALEMBIC_TARGET=head WORKERS=4 \
      $TUNED up -d --wait --force-recreate rest-api grpc-api sync-worker >/dev/null 2>&1
    docker compose exec -T redis redis-cli FLUSHALL >/dev/null
    name="pool_${mode}_${size}"
    # sample the number of Postgres backends once per second while k6 runs
    ( for i in $(seq 1 70); do backends; sleep 1; done > "$OUT/$name.backends.txt" ) &
    docker run --rm --network backend-highload-sis2_default \
      -v "$PWD/loadtest:/scripts" -v "$PWD/proto:/proto" -v "$PWD/$OUT:/results" \
      -e PROTOCOL=grpc -e LIKES=${LIKES:-300} -e READS=${READS:-300} -e DURATION=60s \
      -e USER_BASE=$(( $(date +%s) % 100000 * 10000 + 1500000000 )) \
      -e OUT="/results/$name.json" \
      grafana/k6:1.0.0 run -q --no-color /scripts/db_mix.js > /dev/null 2>&1 || true
    wait
    max=$(sort -n "$OUT/$name.backends.txt" | tail -1)
    python3 - "$OUT/$name.json" "$max" "$mode" "$size" <<'EOF'
import json, sys
path, max_backends, mode, size = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
d = json.load(open(path))
d.update(max_backends=max_backends, mode=mode, pool=size)
json.dump(d, open(path, "w"), indent=2)
print(f"{mode:9} pool {size:2}: failed {d['failed_calls']}/{d['calls']}, backends max {max_backends},"
      f" like p95/p99 {d['like_ms']['p95']:.1f}/{d['like_ms']['p99']:.1f},"
      f" read p95/p99 {d['read_ms']['p95']:.1f}/{d['read_ms']['p99']:.1f} ms")
EOF
  done
done
echo "=== pool sweep done"
