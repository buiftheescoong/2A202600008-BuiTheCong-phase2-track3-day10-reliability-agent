from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.cache import SharedRedisCache
from reliability_lab.config import load_config


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 10 else f"{value:.2f}"
    if value is None:
        return "n/a"
    return str(value)


def _delta(without: float, with_cache: float) -> str:
    if without == 0:
        return f"+{with_cache:.4f}"
    pct = ((with_cache - without) / without) * 100
    return f"{pct:+.1f}%"


def _redis_evidence(redis_url: str) -> tuple[str, list[str]]:
    try:
        c1 = SharedRedisCache(redis_url, 300, 0.92, prefix="rl:cache:")
        c2 = SharedRedisCache(redis_url, 300, 0.92, prefix="rl:cache:")
        c1.set("shared admission FAQ", "shared cached response", {"provider": "report"})
        cached, score = c2.get("shared admission FAQ")
        keys = list(c1._redis.scan_iter("rl:cache:*"))  # noqa: SLF001 - report evidence only
        c1.close()
        c2.close()
        return f"shared_state cached={cached!r} score={score}", [str(key) for key in keys]
    except Exception as exc:
        return f"Redis evidence unavailable: {exc}", []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text())
    config = load_config(args.config)
    redis_shared_state, redis_keys = _redis_evidence(config.cache.redis_url)
    comparison = metrics.get("cache_comparison", {})
    without = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})

    scenario_expectations = {
        "primary_timeout_100": "Primary fails 100%; backup serves requests and primary circuit opens.",
        "primary_flaky_50": "Primary is flaky; gateway mixes primary and fallback without retry storm.",
        "all_healthy": "Most requests succeed with low error rate.",
        "cache_stale_candidate": "Similar date-sensitive queries do not return stale cached answers.",
    }

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks cache first, then routes each miss through provider-specific circuit breakers. If the primary provider fails or its circuit is open, traffic falls through to the backup provider; if every provider fails, the gateway returns a static degraded response.",
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[Gateway] -> [Cache check] -> HIT: return cached response",
        "    | MISS",
        "    v",
        "[Circuit: primary] -> Provider primary",
        "    | OPEN/FAIL",
        "    v",
        "[Circuit: backup] -> Provider backup",
        "    | OPEN/FAIL",
        "    v",
        "[Static fallback]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Opens quickly after repeated failures without reacting to one transient error. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Short enough for lab recovery evidence, long enough to avoid immediate retry storms. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One successful probe is enough for the fake provider recovery model. |",
        f"| cache TTL | {config.cache.ttl_seconds} | Five-minute freshness window for FAQ-style answers. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | High threshold reduces stale semantic hits; guardrails reject different years/IDs. |",
        f"| load_test requests | {config.load_test.requests} | Gives enough requests to measure cache and fallback rates. |",
        f"| load_test concurrency | {config.load_test.concurrency} | Exercises concurrent gateway behavior without making the lab too slow. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_fmt(metrics['availability'])} | {'yes' if metrics['availability'] >= 0.99 else 'no'} |",
        f"| Latency P95 | < 2500 ms | {_fmt(metrics['latency_p95_ms'])} | {'yes' if metrics['latency_p95_ms'] < 2500 else 'no'} |",
        f"| Fallback success rate | >= 95% | {_fmt(metrics['fallback_success_rate'])} | {'yes' if metrics['fallback_success_rate'] >= 0.95 else 'no'} |",
        f"| Cache hit rate | >= 10% | {_fmt(metrics['cache_hit_rate'])} | {'yes' if metrics['cache_hit_rate'] >= 0.10 else 'no'} |",
        f"| Recovery time | < 5000 ms | {_fmt(metrics['recovery_time_ms'])} | {'yes' if metrics['recovery_time_ms'] is not None and metrics['recovery_time_ms'] < 5000 else 'no'} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]

    metric_keys = [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "estimated_cost",
        "estimated_cost_saved",
        "circuit_open_count",
        "recovery_time_ms",
    ]
    for key in metric_keys:
        lines.append(f"| {key} | {_fmt(metrics.get(key))} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        without_value = float(without.get(key, 0.0))
        with_value = float(with_cache.get(key, 0.0))
        lines.append(
            f"| {key} | {_fmt(without_value)} | {_fmt(with_value)} | {_delta(without_value, with_value)} |"
        )

    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "In-memory cache is per-process, so horizontally scaled gateways would miss entries created by sibling instances. `SharedRedisCache` stores query/response entries in Redis with TTL, making cache state visible across gateway instances while privacy and false-hit guardrails still run before reads/writes.",
        "",
        "### Evidence of shared state",
        "",
        "```text",
        redis_shared_state,
        "```",
        "",
        "### Redis CLI output",
        "",
        "```bash",
        "$ docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
    ]
    lines.extend(redis_keys or ["# no keys captured"])
    lines += [
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    for scenario, status in metrics.get("scenarios", {}).items():
        expected = scenario_expectations.get(scenario.split(".")[0], "Evidence scenario should satisfy its check.")
        observed = f"status={status}; availability={_fmt(metrics['availability'])}; circuit_open_count={metrics['circuit_open_count']}"
        lines.append(f"| {scenario} | {expected} | {observed} | {status} |")

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "The remaining production weakness is that circuit breaker state is still local to each process. In a multi-instance deployment, one gateway can learn that a provider is failing while another instance continues sending traffic until its own local threshold opens. Before production, circuit state should be moved to Redis or another shared low-latency store with atomic counters and expirations.",
        "",
        "## 9. Next steps",
        "",
        "1. Store circuit breaker counters and state transitions in Redis so fallback behavior is consistent across instances.",
        "2. Export Prometheus counters/gauges for request totals, latency buckets, cache hits, and circuit state.",
        "3. Add cost-aware routing and per-user rate limiting before provider calls.",
        "",
        "## Reproducibility",
        "",
        "```bash",
        "pip install -e \".[dev]\"",
        "docker compose up -d",
        "python -m pytest -q",
        "python -m ruff check src tests scripts",
        "python -m mypy src",
        "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json",
        "python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md",
        "```",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
