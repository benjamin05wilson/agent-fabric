# Benchmark results

Source directory: `benchmarks/reports/2026-09-02-batch-scheduler/results`

## Environment

- git_revision: `76840f948830cbf925d4fe5b43febcb21af23fd0`
- git_dirty: `True`
- hostname: `vm`
- kernel: `6.18.44-fc-v22`
- cpu_model: `Intel(R) Xeon(R) Processor @ 2.10GHz`
- cpu_count: `4`
- memory_mb: `16075`
- python: `3.11.15`
- postgres: `16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)`
- redis: `7.0.15`
- note: `single host; control plane, datastores, and load generator share the CPUs`

## Scaling tiers

| Label | Workers | Jobs accepted | 429s | Registered in (s) | Drained | Drain (s) | Placements/s | Completion/s | Time-to-start p50/p95/p99 (ms) | End-to-end p50/p95/p99 (ms) | Lost | Non-terminal | Leaked CPU (millis) | Peak RSS api/grpc/outbox/sched (MB) | CPU api/grpc/outbox/sched (% of one core) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| batch | 100 | 10,000 | 0 | 0.0 | yes | 110.1 | 90.8 | 90.8 | 23,516/39,105/40,115 | 24,004/39,569/40,433 | 0 | 0 | 0 | 115/103/84/88 | 51/93/7/41 |
| batch | 1,000 | 10,000 | 0 | 0.2 | yes | 283.7 | 51.7 | 50.4 | 113,599/207,249/217,116 | 114,189/208,202/217,519 | 0 | 0 | 0 | 115/173/88/88 | 20/99/5/32 |
| batch | 10,000 | 10,000 | 0 | n/a | no | 969.6 | 31.9 | 9.8 | n/a/n/a/n/a | n/a/n/a/n/a | 0 | 10,000 | 0 | 115/388/83/101 | 6/101/3/23 |
| batch-bound100 | 1,000 | 10,000 | 0 | 0.2 | yes | 274.0 | 36.5 | 36.5 | 117,720/202,921/207,410 | 118,158/203,305/210,058 | 0 | 0 | 0 | 115/172/84/86 | 21/98/4/16 |

## Redis

| Label | Workers | Connected clients | Blocked | Peak memory | Commands | Rejected |
|---|---|---|---|---|---|---|
| batch | 100 | 11 | 0 | 207.31M | 37577 | 0 |
| batch | 1,000 | 15 | 0 | 207.31M | 354067 | 0 |
| batch | 10,000 | 1276 | 0 | 207.31M | 1058361 | 0 |
| batch-bound100 | 1,000 | 15 | 0 | 207.31M | 1393465 | 0 |

## PostgreSQL statement profile (pg_stat_statements)

### PostgreSQL statements: batch / 100 workers

| Calls | Total ms | Mean ms | Max ms | Rows | Query |
|---|---|---|---|---|---|
| 22,101 | 9728.1 | 0.440 | 55.3 | 22101 | `SELECT workers.id AS workers_id, workers.protocol_version AS workers_protocol_version, workers.worker_version ` |
| 10,000 | 9259.5 | 0.926 | 6.5 | 10000 | `SELECT count(*) AS count_1  FROM runs  WHERE runs.project_id = $1::UUID AND runs.state = $2` |
| 10,000 | 7758.5 | 0.776 | 100.3 | 10000 | `INSERT INTO attempts (id, run_id, worker_id, number, state, lease_token_hash, lease_expires_at, acknowledged_a` |
| 10,000 | 7316.2 | 0.732 | 100.2 | 10000 | `SELECT $2 FROM ONLY "public"."workers" x WHERE "id"::pg_catalog.text OPERATOR(pg_catalog.=) $1::pg_catalog.tex` |
| 1,638 | 4543.7 | 2.774 | 17.9 | 710164 | `SELECT runs.id, runs.project_id, runs.idempotency_key, runs.request_hash, runs.spec, runs.state, runs.priority` |
| 1,711 | 2491.1 | 1.456 | 7.9 | 0 | `SELECT attempts.id, attempts.run_id, attempts.worker_id, attempts.number, attempts.state, attempts.lease_token` |
| 1,638 | 2270.3 | 1.386 | 6.4 | 1637 | `SELECT runs.project_id, count(*) AS count_1  FROM runs  WHERE runs.state IN ($1, $2, $3) GROUP BY runs.project` |
| 1,711 | 1565.0 | 0.915 | 4.9 | 1711 | `SELECT count(*) AS count_1  FROM runs  WHERE runs.state = $1` |
| 410621 | 54834.9 |  |  |  | `TOTAL` |

### PostgreSQL statements: batch / 1,000 workers

| Calls | Total ms | Mean ms | Max ms | Rows | Query |
|---|---|---|---|---|---|
| 80,666 | 19589.6 | 0.243 | 460.9 | 80666 | `SELECT workers.id AS workers_id, workers.protocol_version AS workers_protocol_version, workers.worker_version ` |
| 14,666 | 11837.8 | 0.807 | 232.4 | 14666 | `INSERT INTO attempts (id, run_id, worker_id, number, state, lease_token_hash, lease_expires_at, acknowledged_a` |
| 14,666 | 11245.8 | 0.767 | 232.2 | 14666 | `SELECT $2 FROM ONLY "public"."workers" x WHERE "id"::pg_catalog.text OPERATOR(pg_catalog.=) $1::pg_catalog.tex` |
| 1,778 | 10922.0 | 6.143 | 40.1 | 832973 | `SELECT runs.id, runs.project_id, runs.idempotency_key, runs.request_hash, runs.spec, runs.state, runs.priority` |
| 28,596 | 9030.3 | 0.316 | 299.9 | 28596 | `SELECT attempts.id, attempts.run_id, attempts.worker_id, attempts.number, attempts.state, attempts.lease_token` |
| 10,000 | 8854.6 | 0.885 | 7.1 | 10000 | `SELECT count(*) AS count_1  FROM runs  WHERE runs.project_id = $1::UUID AND runs.state = $2` |
| 2,070 | 4239.1 | 2.048 | 9.2 | 4666 | `SELECT attempts.id, attempts.run_id, attempts.worker_id, attempts.number, attempts.state, attempts.lease_token` |
| 1,778 | 1847.6 | 1.039 | 6.2 | 1777 | `SELECT runs.project_id, count(*) AS count_1  FROM runs  WHERE runs.state IN ($1, $2, $3) GROUP BY runs.project` |
| 852907 | 93475.7 |  |  |  | `TOTAL` |

### PostgreSQL statements: batch / 10,000 workers

| Calls | Total ms | Mean ms | Max ms | Rows | Query |
|---|---|---|---|---|---|
| 359,774 | 20913.9 | 0.058 | 471.0 | 359774 | `SELECT workers.id AS workers_id, workers.protocol_version AS workers_protocol_version, workers.worker_version ` |
| 10,000 | 10995.5 | 1.100 | 15.2 | 10000 | `SELECT count(*) AS count_1  FROM runs  WHERE runs.project_id = $1::UUID AND runs.state = $2` |
| 329,289 | 10462.0 | 0.032 | 33.2 | 329289 | `UPDATE workers SET last_seen_at=$1::TIMESTAMP WITH TIME ZONE WHERE workers.id = $2::VARCHAR` |
| 409 | 4830.1 | 11.810 | 42.4 | 183010 | `SELECT runs.id, runs.project_id, runs.idempotency_key, runs.request_hash, runs.spec, runs.state, runs.priority` |
| 1,800 | 4318.8 | 2.399 | 15.3 | 30485 | `SELECT attempts.id, attempts.run_id, attempts.worker_id, attempts.number, attempts.state, attempts.lease_token` |
| 30,485 | 2198.4 | 0.072 | 4.5 | 944585 | `SELECT attempts.run_id AS attempts_run_id, attempts.id AS attempts_id, attempts.worker_id AS attempts_worker_i` |
| 30,485 | 1700.3 | 0.056 | 2.8 | 30485 | `UPDATE runs SET state=$1, updated_at=$2::TIMESTAMP WITH TIME ZONE WHERE runs.id = $3::UUID` |
| 1,201 | 1674.8 | 1.395 | 7.3 | 1201 | `SELECT count(*) AS count_1  FROM attempts  WHERE attempts.state = $1` |
| 2495250 | 73036.8 |  |  |  | `TOTAL` |

### PostgreSQL statements: batch-bound100 / 1,000 workers

| Calls | Total ms | Mean ms | Max ms | Rows | Query |
|---|---|---|---|---|---|
| 10,000 | 8640.8 | 0.864 | 21.5 | 10000 | `SELECT count(*) AS count_1  FROM runs  WHERE runs.project_id = $1::UUID AND runs.state = $2` |
| 1,082 | 6718.2 | 6.209 | 19.1 | 523542 | `SELECT runs.id, runs.project_id, runs.idempotency_key, runs.request_hash, runs.spec, runs.state, runs.priority` |
| 74,000 | 4699.6 | 0.064 | 40.3 | 74000 | `SELECT workers.id AS workers_id, workers.protocol_version AS workers_protocol_version, workers.worker_version ` |
| 10,000 | 3685.6 | 0.369 | 200.0 | 10000 | `INSERT INTO attempts (id, run_id, worker_id, number, state, lease_token_hash, lease_expires_at, acknowledged_a` |
| 10,000 | 3334.1 | 0.333 | 199.9 | 10000 | `SELECT $2 FROM ONLY "public"."workers" x WHERE "id"::pg_catalog.text OPERATOR(pg_catalog.=) $1::pg_catalog.tex` |
| 1,506 | 2112.0 | 1.402 | 6.9 | 0 | `SELECT attempts.id, attempts.run_id, attempts.worker_id, attempts.number, attempts.state, attempts.lease_token` |
| 54,000 | 1475.5 | 0.027 | 2.6 | 54000 | `UPDATE workers SET last_seen_at=$1::TIMESTAMP WITH TIME ZONE WHERE workers.id = $2::VARCHAR` |
| 10,000 | 1091.5 | 0.109 | 4.3 | 10000 | `INSERT INTO runs (id, project_id, idempotency_key, request_hash, spec, state, priority, retry_safe, max_attemp` |
| 727784 | 41787.6 |  |  |  | `TOTAL` |

## Queue depth and healthy workers over time (10 s samples)

### batch / 100 workers

| t (s) | Queue depth | Healthy workers | Placements so far | Outbox lag (s) | Scheduler RSS (MB) | Gateway RSS (MB) |
|---|---|---|---|---|---|---|
| 0.0 | 0 | 0 | 0 | 0.0 | 79 | 88 |
| 10.1 | 99 | 100 | 150 | 0.1 | 83 | 100 |
| 20.2 | 749 | 100 | 329 | 0.0 | 87 | 100 |
| 30.3 | 1,361 | 100 | 514 | 0.1 | 87 | 100 |
| 40.4 | 1,927 | 100 | 683 | 0.0 | 87 | 103 |
| 50.4 | 2,531 | 100 | 843 | 0.0 | 87 | 103 |
| 60.6 | 3,083 | 100 | 960 | 0.0 | 87 | 103 |
| 70.6 | 3,315 | 100 | 1,099 | 0.1 | 87 | 102 |
| 80.7 | 2,327 | 100 | 1,266 | 0.1 | 87 | 102 |
| 90.8 | 1,321 | 100 | 1,430 | 0.1 | 87 | 103 |
| 100.9 | 346 | 100 | 1,579 | 0.1 | 87 | 103 |
| 109.9 | 0 | 100 | 1,638 | 0.1 | 88 | 102 |

### batch / 1,000 workers

| t (s) | Queue depth | Healthy workers | Placements so far | Outbox lag (s) | Scheduler RSS (MB) | Gateway RSS (MB) |
|---|---|---|---|---|---|---|
| 0.0 | 0 | 0 | 0 | 0.0 | 79 | 88 |
| 10.1 | 636 | 1,000 | 74 | 0.0 | 86 | 162 |
| 20.1 | 1,844 | 1,000 | 138 | 0.1 | 87 | 165 |
| 30.2 | 3,046 | 1,000 | 204 | 0.0 | 87 | 166 |
| 40.3 | 4,243 | 1,000 | 270 | 0.0 | 87 | 167 |
| 50.4 | 5,463 | 1,000 | 330 | 0.0 | 87 | 168 |
| 60.5 | 6,682 | 1,000 | 385 | 0.0 | 87 | 168 |
| 70.6 | 7,445 | 1,000 | 438 | 0.1 | 87 | 168 |
| 80.7 | 7,050 | 1,000 | 508 | 0.1 | 87 | 169 |
| 90.8 | 6,670 | 1,000 | 577 | 0.1 | 87 | 168 |
| 100.8 | 6,247 | 1,000 | 653 | 0.1 | 87 | 169 |
| 110.9 | 5,814 | 1,000 | 720 | 0.1 | 87 | 173 |
| 120.9 | 5,392 | 1,000 | 782 | 0.1 | 87 | 172 |
| 131.0 | 5,028 | 1,000 | 851 | 0.0 | 87 | 173 |
| 141.1 | 4,679 | 1,000 | 901 | 0.1 | 87 | 171 |
| 151.1 | 4,314 | 1,000 | 972 | 0.1 | 87 | 171 |
| 161.2 | 4,006 | 1,000 | 1,040 | 0.1 | 87 | 171 |
| 171.2 | 3,649 | 1,000 | 1,102 | 0.1 | 87 | 170 |
| 181.3 | 3,283 | 1,000 | 1,176 | 0.0 | 87 | 170 |
| 191.4 | 2,925 | 1,000 | 1,244 | 0.1 | 87 | 170 |
| 201.5 | 2,602 | 1,000 | 1,299 | 0.1 | 87 | 170 |
| 211.5 | 2,234 | 1,000 | 1,363 | 0.1 | 87 | 169 |
| 221.6 | 1,905 | 1,000 | 1,431 | 0.1 | 87 | 169 |
| 231.6 | 1,525 | 1,000 | 1,492 | 0.1 | 87 | 169 |
| 241.7 | 1,120 | 1,000 | 1,560 | 0.1 | 87 | 168 |
| 251.7 | 724 | 1,000 | 1,626 | 0.1 | 87 | 168 |
| 261.8 | 340 | 1,000 | 1,692 | 0.1 | 87 | 170 |
| 271.9 | 0 | 1,000 | 1,761 | 0.0 | 88 | 171 |
| 281.9 | 0 | 1,000 | 1,777 | 0.1 | 88 | 170 |
| 283.9 | 0 | 1,000 | 1,778 | 0.1 | 88 | 169 |

### batch / 10,000 workers

| t (s) | Queue depth | Healthy workers | Placements so far | Outbox lag (s) | Scheduler RSS (MB) | Gateway RSS (MB) |
|---|---|---|---|---|---|---|
| 0.0 | 0 | 0 | 0 | 0.0 | 79 | 88 |
| 10.0 | 0 | 0 | 0 | 0.0 | 79 | 253 |
| 20.1 | 0 | 0 | 0 | 0.0 | 79 | 306 |
| 30.1 | 0 | 0 | 0 | 0.0 | 79 | 313 |
| 40.2 | 0 | 0 | 0 | 0.0 | 79 | 314 |
| 50.2 | 0 | 0 | 0 | 0.0 | 79 | 317 |
| 60.2 | 0 | 0 | 0 | 0.0 | 79 | 319 |
| 70.3 | 0 | 0 | 0 | 0.0 | 79 | 320 |
| 80.3 | 0 | 0 | 0 | 0.0 | 79 | 323 |
| 90.4 | 0 | 0 | 0 | 0.0 | 79 | 325 |
| 100.4 | 0 | 0 | 0 | 0.0 | 79 | 326 |
| 110.4 | 0 | 0 | 0 | 0.0 | 79 | 325 |
| 120.5 | 0 | 0 | 0 | 0.0 | 79 | 329 |
| 130.5 | 0 | 0 | 0 | 0.0 | 79 | 330 |
| 140.6 | 0 | 0 | 0 | 0.0 | 79 | 329 |
| 150.6 | 0 | 0 | 0 | 0.0 | 79 | 332 |
| 160.6 | 0 | 0 | 0 | 0.0 | 79 | 332 |
| 170.7 | 0 | 0 | 0 | 0.0 | 79 | 332 |
| 180.7 | 0 | 0 | 0 | 0.0 | 79 | 332 |
| 190.8 | 0 | 0 | 0 | 0.0 | 79 | 336 |
| 200.8 | 0 | 0 | 0 | 0.0 | 79 | 336 |
| 210.8 | 0 | 0 | 0 | 0.0 | 79 | 335 |
| 220.9 | 0 | 0 | 0 | 0.0 | 79 | 338 |
| 230.9 | 0 | 0 | 0 | 0.0 | 79 | 337 |
| 240.9 | 0 | 0 | 0 | 0.0 | 79 | 337 |
| 251.0 | 0 | 0 | 0 | 0.0 | 79 | 340 |
| 261.0 | 0 | 0 | 0 | 0.0 | 79 | 339 |
| 271.1 | 0 | 0 | 0 | 0.0 | 79 | 339 |
| 281.1 | 0 | 0 | 0 | 0.0 | 79 | 343 |
| 291.1 | 0 | 0 | 0 | 0.0 | 79 | 343 |
| 301.2 | 0 | 0 | 0 | 0.0 | 79 | 342 |
| 311.2 | 1,010 | 3,712 | 44 | 0.0 | 84 | 345 |
| 321.4 | 2,508 | 3,712 | 50 | 0.1 | 87 | 345 |
| 331.5 | 3,994 | 3,712 | 56 | 0.0 | 88 | 344 |
| 341.6 | 5,485 | 3,712 | 62 | 0.0 | 88 | 346 |
| 351.8 | 6,993 | 3,712 | 68 | 0.0 | 88 | 346 |
| 361.9 | 8,333 | 3,712 | 74 | 0.0 | 88 | 345 |
| 371.9 | 9,500 | 3,712 | 80 | 0.1 | 89 | 348 |
| 382.0 | 9,500 | 3,712 | 86 | 0.1 | 89 | 347 |
| 392.1 | 9,500 | 3,712 | 92 | 0.0 | 89 | 348 |
| 402.1 | 9,600 | 3,712 | 97 | 0.6 | 89 | 349 |
| 412.2 | 9,600 | 3,712 | 101 | 0.6 | 90 | 349 |
| 422.4 | 9,600 | 3,712 | 106 | 0.6 | 89 | 352 |
| 432.5 | 9,595 | 3,712 | 111 | 0.0 | 90 | 352 |
| 442.6 | 9,500 | 3,712 | 117 | 0.1 | 90 | 351 |
| 452.8 | 9,500 | 3,712 | 122 | 0.1 | 90 | 350 |
| 462.8 | 9,500 | 3,712 | 128 | 0.0 | 90 | 353 |
| 472.9 | 9,500 | 3,712 | 134 | 0.0 | 91 | 354 |
| 483.0 | 9,500 | 3,712 | 140 | 0.1 | 91 | 353 |
| 493.1 | 9,500 | 3,712 | 146 | 0.0 | 91 | 353 |
| 503.3 | 9,500 | 3,712 | 152 | 0.0 | 92 | 356 |
| 513.4 | 9,500 | 3,712 | 158 | 0.1 | 92 | 356 |
| 523.4 | 9,500 | 3,712 | 164 | 0.1 | 92 | 356 |
| 533.5 | 9,600 | 3,712 | 168 | 0.0 | 92 | 356 |
| 543.5 | 9,600 | 3,712 | 172 | 0.6 | 92 | 355 |
| 553.7 | 9,500 | 3,712 | 178 | 0.0 | 93 | 358 |
| 563.7 | 9,500 | 3,712 | 184 | 0.1 | 92 | 358 |
| 573.8 | 9,500 | 3,712 | 190 | 0.1 | 93 | 358 |
| 583.9 | 9,595 | 3,712 | 195 | 0.5 | 93 | 359 |
| 594.0 | 9,500 | 3,712 | 201 | 0.1 | 93 | 365 |
| 604.0 | 9,500 | 3,712 | 207 | 0.1 | 93 | 366 |
| 614.1 | 9,590 | 3,712 | 213 | 0.5 | 94 | 366 |
| 624.2 | 9,500 | 3,712 | 218 | 0.0 | 94 | 367 |
| 634.3 | 9,500 | 3,712 | 224 | 0.0 | 94 | 367 |
| 644.3 | 9,500 | 3,712 | 230 | 0.1 | 94 | 366 |
| 654.4 | 9,500 | 3,712 | 236 | 0.1 | 94 | 370 |
| 664.5 | 9,500 | 3,712 | 242 | 0.1 | 95 | 370 |
| 674.6 | 9,600 | 3,712 | 246 | 0.1 | 95 | 370 |
| 684.6 | 9,600 | 3,712 | 251 | 0.0 | 95 | 370 |
| 694.7 | 9,500 | 3,712 | 256 | 0.1 | 96 | 372 |
| 704.8 | 9,500 | 3,712 | 262 | 0.1 | 96 | 372 |
| 714.9 | 9,595 | 3,712 | 267 | 0.5 | 96 | 371 |
| 725.0 | 9,500 | 3,712 | 273 | 0.1 | 95 | 370 |
| 735.1 | 9,500 | 3,712 | 279 | 0.1 | 96 | 370 |
| 745.2 | 9,500 | 3,712 | 285 | 0.1 | 96 | 374 |
| 755.2 | 9,500 | 3,712 | 290 | 0.1 | 97 | 373 |
| 765.3 | 9,500 | 3,712 | 296 | 0.0 | 96 | 373 |
| 775.3 | 9,500 | 3,712 | 302 | 0.1 | 96 | 372 |
| 785.5 | 9,600 | 3,712 | 306 | 0.1 | 97 | 371 |
| 795.5 | 9,500 | 3,712 | 311 | 0.0 | 98 | 375 |
| 805.6 | 9,500 | 3,712 | 317 | 0.1 | 98 | 375 |
| 815.6 | 9,500 | 3,712 | 322 | 0.1 | 98 | 374 |
| 825.9 | 9,500 | 3,712 | 328 | 0.1 | 98 | 374 |
| 836.0 | 9,500 | 3,712 | 334 | 0.1 | 97 | 373 |
| 846.0 | 9,500 | 3,712 | 340 | 0.1 | 97 | 377 |
| 856.1 | 9,500 | 3,712 | 345 | 0.0 | 98 | 377 |
| 866.2 | 9,500 | 3,712 | 351 | 0.1 | 98 | 378 |
| 876.4 | 9,500 | 3,712 | 357 | 0.1 | 99 | 380 |
| 886.5 | 9,500 | 3,712 | 363 | 0.0 | 98 | 383 |
| 896.6 | 9,500 | 3,712 | 368 | 0.0 | 99 | 382 |
| 906.8 | 9,500 | 3,712 | 374 | 0.0 | 99 | 386 |
| 917.0 | 9,600 | 3,712 | 378 | 0.6 | 100 | 385 |
| 927.1 | 9,600 | 3,712 | 384 | 0.1 | 100 | 385 |
| 937.2 | 9,500 | 3,712 | 389 | 0.1 | 100 | 385 |
| 947.4 | 9,600 | 3,712 | 394 | 0.6 | 100 | 385 |
| 957.6 | 9,500 | 3,712 | 400 | 0.0 | 100 | 385 |
| 967.7 | 9,500 | 3,712 | 406 | 0.0 | 99 | 388 |
| 970.7 | 9,600 | 3,712 | 407 | 0.6 | 101 | 385 |

### batch-bound100 / 1,000 workers

| t (s) | Queue depth | Healthy workers | Placements so far | Outbox lag (s) | Scheduler RSS (MB) | Gateway RSS (MB) |
|---|---|---|---|---|---|---|
| 0.0 | 0 | 0 | 0 | 0.0 | 79 | 88 |
| 10.1 | 1,182 | 1,000 | 66 | 0.0 | 85 | 162 |
| 20.1 | 2,478 | 1,000 | 99 | 0.0 | 85 | 164 |
| 30.2 | 3,799 | 1,000 | 134 | 0.0 | 85 | 162 |
| 40.3 | 5,145 | 1,000 | 163 | 0.1 | 85 | 168 |
| 50.3 | 6,413 | 1,000 | 201 | 0.0 | 85 | 166 |
| 60.4 | 7,638 | 1,000 | 230 | 0.1 | 85 | 164 |
| 70.5 | 7,828 | 1,000 | 261 | 0.1 | 86 | 168 |
| 80.5 | 7,443 | 1,000 | 309 | 0.1 | 86 | 168 |
| 90.6 | 7,028 | 1,000 | 356 | 0.1 | 86 | 167 |
| 100.6 | 6,670 | 1,000 | 385 | 0.1 | 86 | 166 |
| 110.6 | 6,303 | 1,000 | 417 | 0.0 | 86 | 169 |
| 120.7 | 5,920 | 1,000 | 453 | 0.0 | 86 | 168 |
| 130.7 | 5,547 | 1,000 | 523 | 0.0 | 86 | 167 |
| 140.8 | 5,184 | 1,000 | 560 | 0.0 | 86 | 171 |
| 150.8 | 4,782 | 1,000 | 599 | 0.0 | 86 | 168 |
| 160.9 | 4,444 | 1,000 | 634 | 0.1 | 86 | 171 |
| 171.0 | 4,082 | 1,000 | 688 | 0.1 | 86 | 170 |
| 181.0 | 3,720 | 1,000 | 725 | 0.0 | 86 | 170 |
| 191.0 | 3,320 | 1,000 | 772 | 0.0 | 86 | 168 |
| 201.1 | 2,928 | 1,000 | 804 | 0.1 | 86 | 171 |
| 211.2 | 2,528 | 1,000 | 846 | 0.1 | 86 | 171 |
| 221.2 | 2,123 | 1,000 | 874 | 0.1 | 86 | 170 |
| 231.3 | 1,696 | 1,000 | 913 | 0.1 | 86 | 170 |
| 241.3 | 1,276 | 1,000 | 958 | 0.1 | 86 | 169 |
| 251.4 | 871 | 1,000 | 993 | 0.1 | 86 | 168 |
| 261.4 | 463 | 1,000 | 1,041 | 0.0 | 86 | 171 |
| 271.5 | 0 | 1,000 | 1,082 | 0.1 | 86 | 171 |
| 274.5 | 0 | 1,000 | 1,082 | 0.1 | 86 | 149 |

