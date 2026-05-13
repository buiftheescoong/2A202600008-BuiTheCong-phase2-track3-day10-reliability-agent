# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks cache first, then routes each miss through provider-specific circuit breakers. If the primary provider fails or its circuit is open, traffic falls through to the backup provider; if every provider fails, the gateway returns a static degraded response.

```text
User Request
    |
    v
[Gateway] -> [Cache check] -> HIT: return cached response
    | MISS
    v
[Circuit: primary] -> Provider primary
    | OPEN/FAIL
    v
[Circuit: backup] -> Provider backup
    | OPEN/FAIL
    v
[Static fallback]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens quickly after repeated failures without reacting to one transient error. |
| reset_timeout_seconds | 2.0 | Short enough for lab recovery evidence, long enough to avoid immediate retry storms. |
| success_threshold | 1 | One successful probe is enough for the fake provider recovery model. |
| cache TTL | 300 | Five-minute freshness window for FAQ-style answers. |
| similarity_threshold | 0.92 | High threshold reduces stale semantic hits; guardrails reject different years/IDs. |
| load_test requests | 200 | Gives enough requests to measure cache and fallback rates. |
| load_test concurrency | 10 | Exercises concurrent gateway behavior without making the lab too slow. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 0.9962 | yes |
| Latency P95 | < 2500 ms | 319.64 | yes |
| Fallback success rate | >= 95% | 0.9800 | yes |
| Cache hit rate | >= 10% | 0.7475 | yes |
| Recovery time | < 5000 ms | 3503.65 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 800 |
| availability | 0.9962 |
| error_rate | 0.0037 |
| latency_p50_ms | 0.1500 |
| latency_p95_ms | 319.64 |
| latency_p99_ms | 523.49 |
| fallback_success_rate | 0.9800 |
| cache_hit_rate | 0.7475 |
| estimated_cost | 0.0810 |
| estimated_cost_saved | 0.5980 |
| circuit_open_count | 3 |
| recovery_time_ms | 3503.65 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 237.58 | 0.2100 | -99.9% |
| latency_p95_ms | 506.56 | 251.20 | -50.4% |
| estimated_cost | 0.0949 | 0.0323 | -65.9% |
| cache_hit_rate | 0.0000 | 0.7150 | +0.7150 |

## 6. Redis shared cache

In-memory cache is per-process, so horizontally scaled gateways would miss entries created by sibling instances. `SharedRedisCache` stores query/response entries in Redis with TTL, making cache state visible across gateway instances while privacy and false-hit guardrails still run before reads/writes.

### Evidence of shared state

```text
shared_state cached='shared cached response' score=1.0
```

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:1bec4eb00bf5
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails 100%; backup serves requests and primary circuit opens. | status=pass; availability=0.9962; circuit_open_count=3 | pass |
| primary_flaky_50 | Primary is flaky; gateway mixes primary and fallback without retry storm. | status=pass; availability=0.9962; circuit_open_count=3 | pass |
| all_healthy | Most requests succeed with low error rate. | status=pass; availability=0.9962; circuit_open_count=3 | pass |
| cache_stale_candidate | Similar date-sensitive queries do not return stale cached answers. | status=pass; availability=0.9962; circuit_open_count=3 | pass |
| cache_stale_candidate.cache_false_hit_guardrail | Similar date-sensitive queries do not return stale cached answers. | status=pass; availability=0.9962; circuit_open_count=3 | pass |

## 8. Failure analysis

The remaining production weakness is that circuit breaker state is still local to each process. In a multi-instance deployment, one gateway can learn that a provider is failing while another instance continues sending traffic until its own local threshold opens. Before production, circuit state should be moved to Redis or another shared low-latency store with atomic counters and expirations.

## 9. Next steps

1. Store circuit breaker counters and state transitions in Redis so fallback behavior is consistent across instances.
2. Export Prometheus counters/gauges for request totals, latency buckets, cache hits, and circuit state.
3. Add cost-aware routing and per-user rate limiting before provider calls.

## Reproducibility

```bash
pip install -e ".[dev]"
docker compose up -d
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md
```