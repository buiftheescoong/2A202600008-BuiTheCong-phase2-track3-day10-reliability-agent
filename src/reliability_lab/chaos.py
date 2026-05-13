from __future__ import annotations

import copy
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import cast

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            redis_cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
            cache = redis_cache if redis_cache.ping() else ResponseCache(
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive average OPEN-to-CLOSED recovery time from transition logs."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _record_result(metrics: RunMetrics, result: GatewayResponse) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost
    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += 0.001
    if result.route.startswith("fallback:"):
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1
    metrics.latencies_ms.append(result.latency_ms)


def _run_requests(gateway: ReliabilityGateway, queries: list[str], request_count: int, concurrency: int) -> RunMetrics:
    metrics = RunMetrics()
    prompts = [random.choice(queries) for _ in range(request_count)]
    if concurrency <= 1:
        for prompt in prompts:
            _record_result(metrics, gateway.complete(prompt))
        return metrics

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(gateway.complete, prompt) for prompt in prompts]
        for future in as_completed(futures):
            _record_result(metrics, future.result())
    return metrics


def _exercise_recovery(gateway: ReliabilityGateway, reset_timeout_seconds: float) -> None:
    """Run a real recovery probe so transition logs include OPEN -> CLOSED evidence."""
    if not any(breaker.state.value == "open" for breaker in gateway.breakers.values()):
        return
    for provider in gateway.providers:
        provider.fail_rate = 0.0
    time.sleep(reset_timeout_seconds)
    gateway.complete(f"recovery probe {time.time()}")


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = _run_requests(
        gateway,
        queries,
        config.load_test.requests,
        config.load_test.concurrency,
    )
    _exercise_recovery(gateway, config.circuit_breaker.reset_timeout_seconds)

    if scenario.name == "cache_stale_candidate":
        cache = gateway.cache
        if cache is not None:
            cache.set("refund policy for 2024", "old policy")
            cached, _ = cache.get("refund policy for 2026")
            metrics.scenarios["cache_false_hit_guardrail"] = "pass" if cached is None else "fail"

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def _scenario_passed(name: str, result: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return result.circuit_open_count > 0 and result.fallback_success_rate >= 0.9
    if name == "primary_flaky_50":
        return result.circuit_open_count > 0 and result.successful_requests > 0
    if name == "all_healthy":
        return result.availability >= 0.95 and result.error_rate <= 0.05
    if name == "cache_stale_candidate":
        return result.scenarios.get("cache_false_hit_guardrail") == "pass"
    return result.successful_requests > 0


def _merge_metrics(combined: RunMetrics, result: RunMetrics) -> None:
    combined.total_requests += result.total_requests
    combined.successful_requests += result.successful_requests
    combined.failed_requests += result.failed_requests
    combined.fallback_successes += result.fallback_successes
    combined.static_fallbacks += result.static_fallbacks
    combined.cache_hits += result.cache_hits
    combined.circuit_open_count += result.circuit_open_count
    combined.estimated_cost += result.estimated_cost
    combined.estimated_cost_saved += result.estimated_cost_saved
    combined.latencies_ms.extend(result.latencies_ms)
    if result.recovery_time_ms is not None:
        if combined.recovery_time_ms is None:
            combined.recovery_time_ms = result.recovery_time_ms
        else:
            combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2


def _build_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, float]]:
    no_cache_config = copy.deepcopy(config)
    no_cache_config.cache.enabled = False
    with_cache_config = copy.deepcopy(config)
    with_cache_config.cache.enabled = True
    scenario = ScenarioConfig(name="cache_comparison", description="cache on/off comparison")
    without_cache = run_scenario(no_cache_config, queries, scenario).to_report_dict()
    with_cache = run_scenario(with_cache_config, queries, scenario).to_report_dict()
    keys = ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]
    return {
        "without_cache": {key: float(cast(float | int, without_cache[key])) for key in keys},
        "with_cache": {key: float(cast(float | int, with_cache[key])) for key in keys},
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run named scenarios and aggregate their metrics."""
    scenarios = config.scenarios or [ScenarioConfig(name="default", description="baseline run")]
    combined = RunMetrics()
    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = "pass" if _scenario_passed(scenario.name, result) else "fail"
        for evidence_name, status in result.scenarios.items():
            combined.scenarios[f"{scenario.name}.{evidence_name}"] = status
        _merge_metrics(combined, result)

    combined.cache_comparison = _build_cache_comparison(config, queries)
    return combined
