# SIS #3: database before / after (direct SQL, 16 connections)

| Query | before p50 | before p95 | before p99 | after p50 | after p95 | after p99 | p99 gain | before /s | after /s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Q1 like | 0.93 | 1.52 | 2.59 | 1.01 | 1.62 | 2.67 | 1.0x | 15,817 | 14,400 |
| Q3 cold read x1 | 1.36 | 3.78 | 7.97 | 1.40 | 2.95 | 5.80 | 1.4x | 8,741 | 9,915 |
| Q3 cold read x100 | 74.09 | 116.15 | 158.92 | 69.77 | 122.02 | 241.37 | 0.7x | 204 | 200 |
| Q4 liked_by_me x20 | 0.92 | 1.83 | 3.27 | 1.28 | 3.15 | 6.12 | 0.5x | 15,632 | 10,375 |
| Q5 sync 1000 (executemany) | 15.65 | 20.42 | 44.49 | 17.65 | 28.76 | 127.42 | 0.3x | 59,520 | 47,971 |
| Q5 sync 1000 (unnest) | 11.24 | 14.68 | 22.74 | 12.82 | 17.17 | 24.77 | 0.9x | 86,322 | 74,799 |

Times in ms. Q5 rows/s = rows written per second.

- Q5 sync 1000 (executemany): WAL 233 -> 265 KB per batch, HOT updates 94% -> 33%
- Q5 sync 1000 (unnest): WAL 190 -> 206 KB per batch, HOT updates 99% -> 65%
- Buffer cache hit ratio: 97.38% -> 97.01%
- Settings before: {'effective_cache_size': '5242888kB', 'max_wal_size': '1024MB', 'random_page_cost': '4', 'shared_buffers': '163848kB', 'work_mem': '4096kB'}
- Settings after: {'effective_cache_size': '1966088kB', 'max_wal_size': '4096MB', 'random_page_cost': '1.1', 'shared_buffers': '491528kB', 'work_mem': '8192kB'}
- Sizes before: {'post_counters': '4273 MB', 'likes': '1600 MB', 'alembic_version': '24 kB'}
- Sizes after: {'post_counters': '4707 MB', 'alembic_version': '24 kB', 'likes': '0 bytes'}
