# Fixed-rate results: same RPS for REST and gRPC

| Protocol | Operation | target RPS | achieved RPS | errors | p50 ms | p95 ms | p99 ms | mean ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| REST | LikePost | 300 | 299.8 | 1 | 2.72 | 168.59 | 378.06 | 23.8 |
| gRPC | LikePost | 300 | 299.9 | 0 | 2.27 | 42.99 | 308.13 | 11.38 |
| REST | RecordView | 300 | 299.9 | 0 | 2.55 | 3.85 | 44.16 | 3.69 |
| gRPC | RecordView | 300 | 299.9 | 0 | 2.21 | 3.27 | 4.66 | 2.32 |
| REST | GetCounters x1 | 300 | 299.9 | 0 | 2.62 | 3.41 | 9.26 | 2.83 |
| gRPC | GetCounters x1 | 300 | 300.0 | 0 | 2.25 | 3.21 | 4.0 | 2.3 |
| REST | GetCounters x10 | 300 | 300.0 | 0 | 2.7 | 3.42 | 3.87 | 2.72 |
| gRPC | GetCounters x10 | 300 | 299.9 | 0 | 2.35 | 3.29 | 4.47 | 2.47 |
| REST | GetCounters x100 | 300 | 300.0 | 0 | 3.67 | 4.92 | 7.34 | 3.84 |
| gRPC | GetCounters x100 | 300 | 299.9 | 0 | 2.86 | 3.56 | 4.75 | 2.91 |
