#!/bin/sh
# SIS #3: the whole before / after experiment, one command. Takes ~40-60 min (seeding 50M rows).
#
#   sh db/run_sis3.sh
#
# BEFORE = docker-compose.yml only: schema 0001, default Postgres settings, no PgBouncer, no replica.
# AFTER  = + docker-compose.db.yml: schema 0002 (partitions, fillfactor), tuned config, PgBouncer, replica.
#
# Results: benchmark/db/{before,after}.json, explain_{before,after}.txt, mix_*.json, benchmark/db_results.md
set -e
cd "$(dirname "$0")/.."
OUT=benchmark/db
mkdir -p "$OUT"
PY=${PY:-.venv/bin/python}
BASE="docker compose -f docker-compose.yml"
TUNED="docker compose -f docker-compose.yml -f docker-compose.db.yml"
DIRECT=postgresql://postgres:postgres@postgres:5432/counters

psql() { docker compose exec -T postgres psql -U postgres -d counters -v ON_ERROR_STOP=1 "$@"; }
alembic() { docker compose run --rm --no-deps -e DATABASE_URL=$DIRECT rest-api alembic "$@"; }

mix() {  # mix <label> <workers> <protocol>
  docker compose exec -T redis redis-cli FLUSHALL >/dev/null   # every read is a cold key
  docker run --rm --network backend-highload-sis2_default \
    -v "$PWD/loadtest:/scripts" -v "$PWD/proto:/proto" -v "$PWD/$OUT:/results" \
    -e PROTOCOL="$3" -e DURATION=60s -e USER_BASE=$(( $(date +%s) % 100000 * 10000 + 1500000000 )) \
    -e OUT="/results/mix_$1_w$2_$3.json" \
    grafana/k6:1.0.0 run -q --no-color /scripts/db_mix.js | grep '^{' || true
}

# PHASE=after sh db/run_sis3.sh  -> skip BEFORE (already measured), continue with AFTER.
if [ "${PHASE:-all}" != "after" ]; then
echo "=== BEFORE: schema 0001, default settings, direct connections"
$TUNED stop pgbouncer postgres-replica >/dev/null 2>&1 || true
ALEMBIC_TARGET=0001 WORKERS=4 $BASE up -d --wait --build --remove-orphans
alembic downgrade 0001                       # if a previous run left schema 0002
echo "--- seeding 50M posts + 20M likes (several minutes)"
psql -f - < db/seed.sql
psql -f - < db/explain.sql > "$OUT/explain_before.txt" 2>&1
BENCH_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/counters \
  $PY -m benchmark.db_bench --label before > /dev/null
for p in rest grpc; do mix before 4 "$p"; done
echo "--- 8 processes per API, no PgBouncer: 8 x 10 x 2 = 160 connections > max_connections 100"
ALEMBIC_TARGET=0001 WORKERS=8 $BASE up -d --wait
for p in rest grpc; do mix before 8 "$p"; done

fi

echo "=== AFTER: migration 0002, tuned config, PgBouncer, read replica"
ALEMBIC_TARGET=0001 WORKERS=4 $TUNED up -d --wait postgres   # primary restarts with the tuned config
alembic upgrade head                                          # partitions + fillfactor (no-op if done)
ALEMBIC_TARGET=head WORKERS=4 $TUNED up -d --wait --build     # the replica copies the primary here
psql -f - < db/explain.sql > "$OUT/explain_after.txt" 2>&1
BENCH_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/counters \
  $PY -m benchmark.db_bench --label after > /dev/null
# Replication lag, sampled once per second WHILE the load runs (replay_lag, ms).
( for i in $(seq 1 150); do
    psql -At -c "SELECT now()::time(0), coalesce(round(extract(epoch FROM replay_lag) * 1000), 0)
                 FROM pg_stat_replication" 2>/dev/null
    sleep 1
  done > "$OUT/replication_lag.txt" ) &
for p in rest grpc; do mix after 4 "$p"; done
wait
ALEMBIC_TARGET=head WORKERS=8 $TUNED up -d --wait
for p in rest grpc; do mix after 8 "$p"; done
echo "--- the same, but reads go to the primary (no replica): isolates the effect of the replica"
DATABASE_READ_URL= ALEMBIC_TARGET=head WORKERS=8 $TUNED up -d --wait
for p in rest grpc; do mix noreplica 8 "$p"; done

echo "--- experiments: index cost, partitioning layouts, DELETE vs DROP, flag vs watermark"
BENCH_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/counters \
  $PY -m benchmark.db_experiments > /dev/null

$PY -m benchmark.db_bench --report
