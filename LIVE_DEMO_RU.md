# Живое демо: потрогать всё руками

Каждый шаг: **команда → что увидишь → что это значит**. Команды вставляй в терминал из папки проекта.
Объяснение идей: [STUDY_RU.md](STUDY_RU.md).

## Шаг 0. Подготовка (1 раз в каждом новом терминале)

```bash
cd ~/Documents/University/backend-highload-sis2
export COMPOSE_FILE=docker-compose.yml:docker-compose.db.yml
docker compose ps
```
Увидишь 9 контейнеров `Up`: `postgres`, `postgres-replica`, `pgbouncer`, `redis`, `rest-api`,
`grpc-api`, `sync-worker`, `prometheus`, `grafana`.

`COMPOSE_FILE` = «используй оба файла»: базовый (SIS #2) + базу данных SIS #3.
Если стек не запущен: `docker compose up -d --wait`.

---

# SIS #2

## Шаг 1. Swagger: API в браузере
Открой **http://localhost:8000/docs** → там все 5 операций, можно нажать «Try it out».

## Шаг 2. Лайк и идемпотентность
```bash
curl -s "localhost:8000/api/v1/counters?post_ids=5" -H "X-User-Id: 123"; echo
curl -X PUT localhost:8000/api/v1/posts/5/like -H "X-User-Id: 123"
curl -X PUT localhost:8000/api/v1/posts/5/like -H "X-User-Id: 123"     # второй раз!
curl -s "localhost:8000/api/v1/counters?post_ids=5" -H "X-User-Id: 123"; echo
```
**Увидишь:** `likes` выросло **на 1**, а не на 2, и `liked_by_me: true`.
**Значит:** второй лайк ничего не изменил → повтор безопасен (`ON CONFLICT DO NOTHING`).

## Шаг 3. Просмотры и дедупликация
```bash
curl -X POST localhost:8000/api/v1/posts/5/view -H "X-User-Id: 123"
curl -X POST localhost:8000/api/v1/posts/5/view -H "X-User-Id: 123"    # тот же юзер
curl -s "localhost:8000/api/v1/counters?post_ids=5" -H "X-User-Id: 123"; echo
docker compose exec redis redis-cli TTL seen:123:5
```
**Увидишь:** `views` +1, а `TTL` ≈ 3600.
**Значит:** ключ «видел» живёт 1 час (`SET NX EX 3600`); пока он есть, повтор не считается.

## Шаг 4. Батч: 100 писем одной машиной
```bash
curl -s -X POST localhost:8000/api/v1/views:batch -H "Content-Type: application/json" \
  -d '{"views":[{"user_id":1,"post_id":5},{"user_id":2,"post_id":5},{"user_id":1,"post_id":5}]}'; echo
```
**Увидишь:** `{"counted":2}` — третий просмотр — дубликат первого.

## Шаг 5. Заглянуть внутрь Redis
```bash
docker compose exec redis redis-cli HGETALL post:5        # живой счётчик
docker compose exec redis redis-cli SMEMBERS dirty_posts  # кого worker ещё не сохранил
```
Сразу после шага 4 `dirty_posts` содержит `5`. Через ≤ 5 с — пусто (worker забрал).

## Шаг 6. Worker: Redis → Postgres
```bash
docker compose logs -f sync-worker          # Ctrl+C чтобы выйти
```
Сделай лайк в другом терминале → в логе появится `synced 1 posts`. Проверь Postgres:
```bash
docker compose exec postgres psql -U postgres -d counters -c "SELECT * FROM post_counters WHERE post_id = 5"
```
Те же числа, что в Redis.

## Шаг 7. gRPC — те же данные
```bash
source .venv/bin/activate
python -c "
import asyncio
from clients.grpc_client import GrpcCounterClient
async def m():
    async with GrpcCounterClient('localhost:50051') as c:
        print(await c.get_counters(user_id=123, post_ids=[5]))
asyncio.run(m())"
```
**Увидишь:** те же `likes / views / liked_by_me`, что дал REST. Одна логика — две двери.

## Шаг 8. Тесты
```bash
source .venv/bin/activate
pytest -v           # 59 passed, ~25 с
```
Тесты сами поднимают отдельные Postgres и Redis (testcontainers) — живой стек не трогают.
Интересные: `test_retry_after_lost_response_does_not_count_twice` (потерянный ответ + повтор),
`test_slow_server_gives_deadline_exceeded_fast` (таймаут).

## Шаг 9. Нагрузка + Grafana (смотреть графики вживую)
1. Открой **http://localhost:3000/d/t7-counter** (Grafana).
2. В терминале — нагрузка 5 000 инкрементов/с на 60 секунд:
```bash
docker run --rm --network backend-highload-sis2_default \
  -v "$PWD/loadtest:/scripts" -v "$PWD/proto:/proto" -v "$PWD/benchmark:/results" \
  -e RATE=5000 -e DURATION=60s -e OUT=/results/_demo.json \
  -e USER_BASE=$(( $(date +%s) % 100000 * 10000 + 1500000000 )) \
  grafana/k6:1.0.0 run /scripts/increments.js
```
3. На графиках: запросы/с растут, p50/p99 задержки. В конце k6 печатает JSON:
   `achieved_increments_per_s`, `failed_calls`, `view_call_ms.p99`, `consistency_lag_ms`
   (через сколько мс новый счётчик стал виден).

Первые ~10 секунд могут быть ошибки — это **прогрев** (Redis ещё не знает посты, пул соединений
растёт). Поэтому бенчмарк ([benchmark/run_load.py](benchmark/run_load.py)) делает 10 с разгона
перед замером.

⚠️ **20K вживую на ноутбуке:** поставь `RATE=20000` только на **зарядке и холодном** ноутбуке.
Официальный результат (20 001/с, 0 ошибок, p99 13 мс) — в [benchmark/load_results.md](benchmark/load_results.md).
28.11 на батарее после нескольких прогонов подряд получилось 14.5–18.7K с ошибками: k6, оба API,
Redis и Postgres делят одни 8 ядер, и ноутбук сбрасывает частоту. Это ограничение стенда, а не кода.

---

# SIS #3

## Шаг 10. Войти в Postgres
```bash
docker compose exec postgres psql -U postgres -d counters
```
Дальше команды внутри `psql` (выход — `\q`).

## Шаг 11. Партиции
```sql
\d+ likes
```
**Увидишь:** `Partition key: HASH (user_id)` и 16 партиций `likes_p00 … likes_p15`.

```sql
EXPLAIN SELECT * FROM likes WHERE user_id = 7 AND post_id = 42;
```
**Увидишь:** в плане **одна** партиция (`likes_pXX`), не 16 → это partition pruning.

## Шаг 12. EXPLAIN ANALYZE: индекс есть / нет
```sql
-- 1) по ключу (user_id, post_id) — быстро
EXPLAIN (ANALYZE, BUFFERS) SELECT post_id FROM likes WHERE user_id = 12345;

-- 2) только по post_id — индекса нет, медленно (читает весь ключ всех 16 партиций)
EXPLAIN (ANALYZE, BUFFERS) SELECT user_id FROM likes WHERE post_id = 4242 LIMIT 20;
```
Сравни `Execution Time` и `Buffers: read=`. Во втором случае — сотни мс и десятки тысяч страниц.

Хочешь увидеть, как индекс это чинит (займёт ~1 мин, потом удалим):
```sql
CREATE INDEX demo_post ON likes (post_id);
EXPLAIN (ANALYZE, BUFFERS) SELECT user_id FROM likes WHERE post_id = 4242 LIMIT 20;
DROP INDEX demo_post;      -- T7 этот индекс не нужен: он замедляет запись
```
Все запросы T7 разом: `\q`, потом
`docker compose exec -T postgres psql -U postgres -d counters -f - < db/explain.sql`

## Шаг 13. HOT updates (fillfactor 80)
```sql
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0 * n_tup_hot_upd / nullif(n_tup_upd, 0)) AS hot_pct
FROM pg_stat_user_tables WHERE relname = 'post_counters';
```
`hot_pct` = какая доля обновлений не трогала индекс (дешёвые обновления).

## Шаг 14. PgBouncer — «вахтёр»
```bash
docker compose exec -e PGPASSWORD=postgres postgres psql -h pgbouncer -p 6432 -U postgres pgbouncer -c "SHOW POOLS"
```
**Смотри колонки:** `cl_active` — клиентов (API) подключено к PgBouncer;
`sv_active + sv_idle` — **реальных** соединений к Postgres. Клиентов много, реальных мало.
`pool_mode = transaction`.

```bash
docker compose exec postgres psql -U postgres -d counters -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='counters' AND backend_type='client backend'"
```
Реальных соединений с Postgres — единицы, хотя у API 8 процессов × пул 10.

## Шаг 15. Read replica
```bash
# отставание реплики в байтах (0 = полностью догнала)
docker compose exec postgres psql -U postgres -At -c \
  "SELECT state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS bytes_behind FROM pg_stat_replication"

# сделай лайк — и через секунду он уже на реплике
curl -X PUT localhost:8000/api/v1/posts/5/like -H "X-User-Id: 999"; sleep 6
docker compose exec postgres-replica psql -U postgres -d counters -c "SELECT likes FROM post_counters WHERE post_id = 5"

# реплика только для чтения
docker compose exec postgres-replica psql -U postgres -d counters -c "UPDATE post_counters SET likes = 0 WHERE post_id = 5"
```
**Увидишь:** `streaming | 0`; на реплике то же число; запись → `ERROR: cannot execute UPDATE in a read-only transaction`.

## Шаг 16. Готовые результаты замеров
| Что | Файл |
|---|---|
| Все цифры SIS #3 с объяснениями | [SIS3.md](SIS3.md) |
| Планы EXPLAIN до / после | [benchmark/db/explain_before.txt](benchmark/db/explain_before.txt), [explain_after.txt](benchmark/db/explain_after.txt) |
| Индекс: 433 → 5.6 мс | [benchmark/db/explain_index.txt](benchmark/db/explain_index.txt), [index.json](benchmark/db/index.json) |
| Пулы соединений | `benchmark/db/pool_direct_*.json`, `pool_pgbouncer_*.json` |
| 20K (SIS #2) | [benchmark/load_results.md](benchmark/load_results.md), [load_chart.png](benchmark/load_chart.png) |

Красиво посмотреть JSON: `python3 -m json.tool benchmark/db/index.json`

---

## Если что-то сломалось
- Контейнер упал: `docker compose up -d --wait`
- Посмотреть ошибки: `docker compose logs --tail 50 grpc-api`
- Остановить всё (данные останутся): `docker compose down`  ← **без `-v`**, иначе удалятся 50 млн постов
