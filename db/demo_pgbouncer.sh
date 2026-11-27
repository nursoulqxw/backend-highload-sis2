#!/bin/sh
# PgBouncer demo: the SAME high load (1000 likes/s + 1500 reads/s, 30 s) with 8 server
# processes per API, once with direct connections and once through PgBouncer.
# Then it counts the errors in the server logs by type.
#
#   sh db/demo_pgbouncer.sh
#
# Direct:    8 processes x 2 APIs x up to 10 connections = 160 > max_connections 100
#            -> "too many clients" errors once the pools grow under load.
# PgBouncer: the APIs open as many client connections as they like; PgBouncer keeps
#            at most 40 real connections per database -> no "too many clients".
set -e
cd "$(dirname "$0")/.."
OUT=benchmark/db
BASE="docker compose -f docker-compose.yml"
TUNED="docker compose -f docker-compose.yml -f docker-compose.db.yml"

run() {  # run <label> <compose command...>
  label=$1; shift
  docker compose exec -T redis redis-cli FLUSHALL >/dev/null
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker run --rm --network backend-highload-sis2_default \
    -v "$PWD/loadtest:/scripts" -v "$PWD/proto:/proto" -v "$PWD/$OUT:/results" \
    -e PROTOCOL=grpc -e LIKES=1000 -e READS=1500 -e DURATION=30s \
    -e USER_BASE=$(( $(date +%s) % 100000 * 10000 + 1500000000 )) \
    -e OUT="/results/pgbouncer_demo_$label.json" \
    grafana/k6:1.0.0 run -q --no-color /scripts/db_mix.js | grep '^{' || true
  echo "errors in the grpc-api log ($label):"
  "$@" logs grpc-api --since "$since" 2>&1 \
    | grep -oE "TooManyConnectionsError|too many clients|TimeoutError|ConnectionDoesNotExistError|QueryCanceledError" \
    | sort | uniq -c | tee "$OUT/pgbouncer_demo_$label.errors.txt"
}

echo "=== direct connections, 8 processes"
$TUNED stop pgbouncer postgres-replica >/dev/null 2>&1 || true
ALEMBIC_TARGET=0001 WORKERS=8 $BASE up -d --wait --force-recreate rest-api grpc-api sync-worker
run direct $BASE

echo "=== through PgBouncer, 8 processes (reads from the primary, to isolate PgBouncer)"
DATABASE_READ_URL= ALEMBIC_TARGET=0001 WORKERS=8 $TUNED up -d --wait --force-recreate pgbouncer rest-api grpc-api sync-worker
run pgbouncer $TUNED
echo "=== demo done"
