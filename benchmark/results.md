# Benchmark results: REST/JSON vs gRPC

- Date: 2026-10-30 11:57
- Machine: Linux-7.0.14-linuxkit-aarch64-with-glibc2.41, aarch64, Python 3.12.16
- Load: 500 warm-up + 10000 measured requests per operation, concurrency 50
- Servers: `docker compose` (rest-api: 1 Uvicorn process, grpc-api: 1 process), same Postgres + Redis, same `app/core` code
- Clients: `clients/rest_client.py` (httpx, HTTP/1.1 keep-alive) and `clients/grpc_client.py` (grpcio, HTTP/2), both with deadline 0.5 s + retries

## All numbers

| Protocol | Operation | p50 ms | p95 ms | p99 ms | mean ms | RPS | errors | request B | response B |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| REST | LikePost | 40.1 | 187.43 | 323.7 | 60.81 | 812.7 | 11 | 0 | 0 |
| gRPC | LikePost | 14.28 | 18.63 | 24.29 | 14.87 | 3355.3 | 0 | 11 | 0 |
| REST | RecordView | 42.91 | 204.95 | 348.98 | 65.53 | 743.3 | 32 | 0 | 0 |
| gRPC | RecordView | 14.21 | 15.59 | 18.1 | 14.28 | 3492.9 | 0 | 11 | 0 |
| REST | GetCounters x1 | 38.17 | 152.49 | 234.73 | 53.92 | 924.4 | 1 | 0 | 64 |
| gRPC | GetCounters x1 | 13.5 | 14.52 | 16.18 | 13.65 | 3653.4 | 0 | 12 | 11 |
| REST | GetCounters x10 | 46.9 | 203.43 | 321.15 | 68.29 | 719.0 | 22 | 0 | 629 |
| gRPC | GetCounters x10 | 20.0 | 21.22 | 23.67 | 20.17 | 2473.4 | 0 | 47 | 109 |
| REST | GetCounters x100 | 89.61 | 273.64 | 411.31 | 113.99 | 424.7 | 69 | 0 | 6281 |
| gRPC | GetCounters x100 | 85.66 | 167.11 | 360.91 | 98.77 | 502.8 | 9 | 407 | 1098 |

## gRPC compared to REST

How many times gRPC is better (bigger = better for gRPC; below 1.0x = REST is better).

| Operation | p50 faster | p99 faster | more RPS | response smaller | REST p99 < 100 ms | gRPC p99 < 100 ms |
|---|---:|---:|---:|---:|:---:|:---:|
| LikePost | 2.8x | 13.3x | 4.1x | - | NO | yes |
| RecordView | 3.0x | 19.3x | 4.7x | - | NO | yes |
| GetCounters x1 | 2.8x | 14.5x | 4.0x | 5.8x | NO | yes |
| GetCounters x10 | 2.3x | 13.6x | 3.4x | 5.8x | NO | yes |
| GetCounters x100 | 1.0x | 1.1x | 1.2x | 5.7x | NO | NO |

## Notes

- Payload = body only, in bytes. REST sends `user_id` in the `X-User-Id` header and `post_id` in the URL, so its request body is 0 bytes. LikePost and RecordView answer 204 / Empty, so their response body is 0 bytes in both protocols.
- The biggest size difference is in GetCounters: JSON repeats the field names (`"post_id"`, `"likes"`, ...) for every post; Protobuf sends small field numbers and compact numbers (varints).
- GetCounters x100: here the server work (100 Redis reads + 1 SQL query) is most of the time, so the two protocols are close, and its p99 changes a lot from run to run.
- errors = calls that did not finish inside the client deadline (0.5 s, all retries included). They are counted, but not included in the latency numbers.
- The benchmark runs as a container in the same Docker network as the servers. On a Mac, going through Docker Desktop port forwarding made REST (50 TCP connections) 3-4x slower, but not gRPC (1 HTTP/2 connection), so that would not be a fair test.
- Latency includes the client library. httpx is written in Python; the gRPC client is written in C. At concurrency 50 the httpx client uses a full CPU core, which is a big part of the REST tail latency (p99). This is part of the real cost of each protocol in Python.

![Latency](latency_p50_p99.png)

![Payload size](payload_size.png)
