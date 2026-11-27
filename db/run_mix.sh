#!/bin/sh
# SIS #3 end-to-end before / after at a load the OLD setup can survive.
# (At 1000 likes/s + 1500 reads/s both setups were overloaded -> nothing to compare.)
#
#   sh db/run_mix.sh          # needs the seeded DB; ~20 min
#
# Order: AFTER first (the DB is already migrated), then downgrade to 0001 for BEFORE.
# Results: benchmark/db/mix2_<label>_w<workers>.json
set -e
cd "$(dirname "$0")/.."
OUT=benchmark/db
LIKES=${LIKES:-300}
READS=${READS:-300}
BASE="docker compose -f docker-compose.yml"
TUNED="docker compose -f docker-compose.yml -f docker-compose.db.yml"
DIRECT=postgresql://postgres:postgres@postgres:5432/counters

mix() {  # mix <label> <workers>
  docker compose exec -T redis redis-cli FLUSHALL >/dev/null   # every read is a cold key
  docker run --rm --network backend-highload-sis2_default \
    -v "$PWD/loadtest:/scripts" -v "$PWD/proto:/proto" -v "$PWD/$OUT:/results" \
    -e PROTOCOL=grpc -e LIKES="$LIKES" -e READS="$READS" -e DURATION=60s \
    -e USER_BASE=$(( $(date +%s) % 100000 * 10000 + 1500000000 )) \
    -e OUT="/results/mix2_$1_w$2.json" \
    grafana/k6:1.0.0 run -q --no-color /scripts/db_mix.js | grep '^{' || true
}

echo "=== AFTER (tuned config, PgBouncer with hard limit, replica)"
$TUNED build pgbouncer >/dev/null
$TUNED up -d --wait --force-recreate postgres postgres-replica pgbouncer   # new configs
ALEMBIC_TARGET=head WORKERS=4 $TUNED up -d --wait --force-recreate rest-api grpc-api sync-worker
mix after 4
ALEMBIC_TARGET=head WORKERS=8 $TUNED up -d --wait
mix after 8
DATABASE_READ_URL= ALEMBIC_TARGET=head WORKERS=8 $TUNED up -d --wait
mix noreplica 8

echo "=== BEFORE (schema 0001, default config, direct connections)"
$TUNED stop pgbouncer postgres-replica >/dev/null
ALEMBIC_TARGET=0001 WORKERS=4 $BASE up -d --wait --force-recreate --remove-orphans postgres
docker compose run --rm --no-deps -e DATABASE_URL=$DIRECT rest-api alembic downgrade 0001
ALEMBIC_TARGET=0001 WORKERS=4 $BASE up -d --wait --force-recreate rest-api grpc-api sync-worker
mix before 4
ALEMBIC_TARGET=0001 WORKERS=8 $BASE up -d --wait
mix before 8
echo "=== done"
