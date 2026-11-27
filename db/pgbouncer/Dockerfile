# PgBouncer from Alpine packages (1.21+ supports prepared statements in transaction mode).
FROM alpine:3.20
RUN apk add --no-cache pgbouncer postgresql16-client \
    && (id pgbouncer >/dev/null 2>&1 || adduser -D -H -s /sbin/nologin pgbouncer)  # it refuses to run as root
COPY pgbouncer.ini userlist.txt /etc/pgbouncer/
USER pgbouncer
EXPOSE 6432
CMD ["pgbouncer", "/etc/pgbouncer/pgbouncer.ini"]
