#!/bin/sh
# Read replica: on the first start, copy the primary with pg_basebackup, then stream WAL.
# -R writes standby.signal + primary_conninfo, so Postgres starts as a hot standby.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h postgres -U postgres -q; do sleep 1; done
  echo "replica: copying the primary (pg_basebackup)..."
  PGPASSWORD=postgres pg_basebackup -h postgres -U postgres -D "$PGDATA" -R -X stream -c fast -P
  chmod 700 "$PGDATA"
fi
exec docker-entrypoint.sh postgres "$@"
